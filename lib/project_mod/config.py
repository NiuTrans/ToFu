"""Project Co-Pilot configuration, constants and shared state."""

import os
import threading

from contextlib import contextmanager
import contextvars
from types import MappingProxyType

# A task owns its policy; the LRU is only a projection. Each call freezes the
# policy so detached/background eviction can never broaden authorization.
_execution_root_view = contextvars.ContextVar('project_execution_root_view', default=None)


def _freeze_root_policy(key, policy):
    with _lock:
        roots = MappingProxyType({name: MappingProxyType(dict(state))
                                 for name, state in policy['roots'].items()})
        return (key, policy['primaryPath'], roots, policy)


def _scoped_root_policy(key):
    view = _execution_root_view.get()
    return view if view is not None and view[0] == key else None


@contextmanager
def execution_root_scope(key, policy):
    """Per-call authorization snapshot, independent of live-task/LRU pinning."""
    token = _execution_root_view.set(_freeze_root_policy(key, policy))
    try:
        yield
    finally:
        _execution_root_view.reset(token)


def build_root_policy(primary, extras=None, readonly_paths=None):
    """Build collision-safe labels with the same exact-root access contract."""
    # Declared roots and readonly fences must expand user paths before comparison.
    declared = [os.path.abspath(os.path.expanduser(path))
                for path in [primary, *(extras or [])]]
    primary = declared[0]
    readonly = {os.path.realpath(path) for path in _normalise_readonly_set(readonly_paths)}
    declared_real = {os.path.realpath(path) for path in declared}
    roots = {}
    for path in declared:
        if any(state['path'] == path for state in roots.values()):
            continue
        original = os.path.basename(path) or 'root'
        name, suffix = original, 2
        while name.lower() in {label.lower() for label in roots}:
            name, suffix = f'{original}_{suffix}', suffix + 1
        real = os.path.realpath(path)
        protected = real in readonly or any(
            real.startswith(fence.rstrip(os.sep) + os.sep) and fence not in declared_real
            for fence in readonly)
        roots[name] = _make_root_state(path, 'ro' if protected else 'rw')
    return {'primaryPath': primary, 'roots': roots}


def register_root_policy(key, policy):
    """Project task-owned policy into the bounded cache, preserving labels."""
    with _lock:
        _conv_roots[key] = policy['roots']
        _conv_roots.move_to_end(key)
        _conv_primary[key] = policy['primaryPath']
        cwd = _conv_cwd.get(key)
        if cwd and not _cwd_within_conv_roots(key, cwd):
            _conv_cwd.pop(key, None)
        _evict_conv_roots_over_cap()


from lib.log import get_logger

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════

IGNORE_DIRS = {
    '.git', '.tofu', 'node_modules', '__pycache__', '.venv', 'venv', 'env',
    '.idea', '.vscode', '.next', 'dist', 'build', '.cache',
    '.tox', '.mypy_cache', '.pytest_cache', 'target', 'vendor',
    '.bundle', 'coverage', '.nyc_output', '.eggs', '.sass-cache',
    'bower_components', '.parcel-cache', '.turbo', '.vercel',
    '.output', '.nuxt', '.svelte-kit', '.angular', 'obj', 'bin',
    '.project_indexes',
    # Bulk runtime/output dirs that explode rg's scan time on FUSE/NFS.
    #   Mirrors the project's .gitignore but also applies in non-git roots
    #   (rg only auto-respects .gitignore inside a .git repo, and we're
    #   often run from exported/copied trees with no .git).  Keeping these
    #   here ensures the same exclusion via rg's `-g '!dir/'` AND grep's
    #   `--exclude-dir` paths in _build_rg_cmd / _build_grep_cmd.
    'logs', 'data', '.project_sessions', 'swebench_workdir',
    'abtest_workdir', '.eval-runs', 'eval-runs', 'evaluation_results',
    'sb-cli-reports', 'jobs', 'trials', 'overleaf_cache', '.ruff_cache',
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
MAX_GREP_RESULTS = 50
LINE_COUNT_LIMIT = 50_000        # skip line counting for files above this
# Per-project undo/redo history store. Resolved through the single runtime-base
# authority so a relocated / frozen / read-only install writes it next to the DB
# instead of under a read-only <repo>/lib. Byte-identical to the legacy
# <repo>/lib/.project_sessions for an in-tree install (see project_sessions_root).
try:
    from lib.runtime.runtime_paths import project_sessions_root as _sessions_root
    SESSIONS_DIR = _sessions_root()
except Exception as _rp_e:  # pragma: no cover — defensive (keeps import robust)
    logger.debug('[Config] project_sessions_root() unavailable, using in-tree: %s', _rp_e)
    SESSIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                '.project_sessions')

MAX_COMMAND_TIMEOUT = None      # no timeout limit for run_command
MAX_COMMAND_OUTPUT  = 100_000   # max chars of command output to return
SHELL_PREFIX = os.environ.get('SHELL_PREFIX', '')  # e.g. 'source ~/.bashrc &&'

# Dangerous command patterns to block
# Includes both Unix and Windows equivalents for cross-platform safety.
#
# NOTE: there is deliberately NO ``rm -rf /`` pattern here. Delete commands
# are guarded by the argument-PARSING ``_is_catastrophic_delete`` (depth
# rule: ``/`` / ``/mnt`` / any <2-component target refused, scoped deep
# paths allowed — plus workspace containment for restricted principals),
# which runs in every enforcement point (run_command, desktop agent,
# safety pre-hook). This list used to carry the blunt regex
# ``\brm\s+-rf\s+/``, which fired on EVERY absolute-path delete: it
# hard-blocked legitimate scoped cleanup such as ``rm -rf /tmp/wt_fill``
# (a temp git-worktree path an agent is expected to manage) with the
# generic "matches dangerous pattern" error BEFORE the parser could allow
# it — while catching nothing the parser doesn't catch better (flag-order
# smuggles like ``rm /mnt -r -f``, ``sudo rm -rf /``). Do not re-add a
# substring/regex delete rule here; extend the parser instead.
DANGEROUS_PATTERNS = [
    # ── Unix ──
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

# Data / bulk files — not binary but not worth reading in full
DATA_EXTENSIONS = {
    '.jsonl', '.ndjson', '.csv', '.tsv', '.parquet',
    '.log', '.logs', '.out', '.err',
    '.sql', '.dump',
    '.xml', '.xsd', '.dtd',
    '.arff', '.sav', '.rec', '.ftr', '.feather',
}
# Max chars returned to LLM for data files in tool_read_file


# ═══════════════════════════════════════════════════════
#  State
# ═══════════════════════════════════════════════════════

_lock = threading.RLock()
if hasattr(os, 'register_at_fork'):
    # Python's RLock fork hook resets ownership in place, preserving the aliases
    # imported by other modules. Never acquire it before fork: its owner may be
    # another parent thread waiting for the forking thread. Leave state and
    # invalidatable journal projections intact; disk remains authoritative.
    os.register_at_fork(after_in_child=_lock._at_fork_reinit)

_state = {
    'path': None, 'tree': None,
    'fileCount': 0, 'dirCount': 0, 'totalSize': 0,
    'languages': {}, 'scannedAt': 0,
    # Async scanning
    'scanning': False, 'scanProgress': '', 'scanDetail': '',
    # Modification history for undo (后悔药)
    'sessionId': None, 'modifications': [],
}

# ═══════════════════════════════════════════════════════
#  Multi-Root Workspace Support
# ═══════════════════════════════════════════════════════
# Each root is stored as:  { name: { path, tree, fileCount, ... } }
# The _state above remains the "primary" root for backward compat.
# _roots dict stores *all* roots including the primary.
#
# ═══════════════════════════════════════════════════════
#  Per-Conversation Root Registries (2026-05-05 fix)
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

# ═══════════════════════════════════════════════════════
#  Sticky per-conversation working directory (2026-07-09)
# ═══════════════════════════════════════════════════════
# conv_id → last-known abs cwd for run_command. This is DERIVED, stateless
# session affinity — NOT a persistent shell and NOT an env snapshot. It exists
# purely to stop the model burning tokens re-emitting `cd <project>` / absolute
# `python`/`pip` on every single run_command call: once it navigates (an
# explicit ``working_dir`` or a trailing ``cd`` inside the command), subsequent
# calls with no ``working_dir`` resume from there. The env/venv is still fully
# re-derived per call by ``_get_cmd_env`` (the CONDA/pip contamination guard +
# portable_sandbox jail are untouched), so there is no mutable state to corrupt,
# leak across tenants, or scrub.
#
# HARD ISOLATION INVARIANT: a cwd is only remembered when it stays INSIDE one of
# that conv's registered roots (``get_conv_roots``). A ``cd /etc`` or a hop into
# another app's tree is rejected and the anchor is left unchanged — app A's cwd
# can never leak into app B, matching the strict per-conv isolation of the root
# registries. Keyed and LRU-evicted exactly like _conv_roots; cleared together.
_conv_cwd: _collections.OrderedDict = _collections.OrderedDict()


def _make_root_state(abs_path, access='rw'):
    """Create a fresh per-root state dict.

    ``access`` is the per-root write policy: ``'rw'`` (default, writable) or
    ``'ro'`` (read-only — reads/greps/find are allowed, but every write,
    edit, and destructive run_command targeting this root is
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
    view = _scoped_root_policy(conv_id)
    if view is not None:
        return {name: dict(state) for name, state in view[2].items()}
    with _lock:
        if conv_id:
            return {n: dict(s) for n, s in _conv_roots.get(conv_id, {}).items()}
        return {n: dict(s) for n, s in _roots.items()}


def get_root_path(name, conv_id=None):
    """Get the absolute path of a named root, or None.

    If ``conv_id`` is given, look it up in that conv's registry first;
    otherwise (or if not found) fall back to the shared global registry.
    """
    view = _scoped_root_policy(conv_id)
    if view is not None:
        return next((state['path'] for label, state in view[2].items()
                     if label.lower() == name.lower()), None)
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
        if conv_id:
            return None
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
    policy = build_root_policy(primary_path, extras, readonly_paths)
    abs_primary = policy['primaryPath']
    extras_list = list(extras or [])
    with _lock:
        conv_map = policy['roots']
        # LRU eviction: drop oldest if over cap.  Re-insertion moves an
        # existing conv to the end (most-recent).
        if conv_id in _conv_roots:
            _conv_roots.move_to_end(conv_id)
        _conv_roots[conv_id] = conv_map
        _conv_primary[conv_id] = abs_primary

        cwd = _conv_cwd.get(conv_id)
        if cwd and not _cwd_within_conv_roots(conv_id, cwd):
            _conv_cwd.pop(conv_id, None)
        # LRU eviction that PINS live-task convs (never evict a conv whose
        # task is mid-flight — see _evict_conv_roots_over_cap). The conv we
        # just registered is newest, so it is never the victim.
        _evict_conv_roots_over_cap()
    logger.debug('[Config] set_conv_roots conv=%s primary=%s extras=%d names=%s',
                 conv_id[:12] if conv_id else '?',
                 abs_primary, len(extras_list), list(conv_map.keys()))


def add_conv_root(conv_id, path, name=None, access='rw'):
    """Add ONE extra root into a conversation's OWN scoped registry.

    The per-conv equivalent of :func:`add_project_root` for the global
    ``_roots``. Used by the absolute-path-write auto-register path so a root
    the running task just expanded into is resolvable via a subsequent
    ``newroot:rel/path`` namespaced write IN THE SAME TASK — otherwise the
    conv-scoped resolver (which, per the 2026-05-05 isolation fix, does NOT
    fall through to the global ``_roots``) would raise
    ``UnknownWorkspaceRootError`` for a root that only landed in the global
    registry.

    Isolation invariants (deliberately conservative):
      * Only the conversation named by ``conv_id`` is touched — never another
        conv's map, never the global ``_roots``.
      * A registry is only MUTATED, never CREATED here: if the conv has no
        existing entry the call is a no-op (returns ``None``). A background
        task must not conjure a conv registry — that would flip the
        conv-scoped resolver into strict-isolation mode for a conv the UI
        never wired a project to.
      * Re-adding the same path refreshes its access flag and returns the
        existing name (idempotent), mirroring ``add_project_root``.

    Args:
        conv_id: Conversation identifier (must already own a registry).
        path:    Directory path to add (abspath'd / expanduser'd here).
        name:    Preferred short name. Defaults to the directory basename.
            Honoured when free in the conv map so the conv-registry name
            agrees with the global-registry name the ``workspace_root_added``
            event advertises; deduped with a ``_N`` suffix on collision.
        access:  ``'rw'`` (default) or ``'ro'`` write policy for this root.

    Returns:
        The assigned root name (str), or ``None`` when there is nothing to do
        (no conv_id, conv has no registry, or path invalid).
    """
    if not conv_id:
        return None
    abs_path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(abs_path):
        return None
    _access = 'ro' if access == 'ro' else 'rw'
    with _lock:
        view = _scoped_root_policy(conv_id)
        conv_map = view[3]['roots'] if view is not None else _conv_roots.get(conv_id)
        if conv_map is None:
            # No conv registry → nothing to extend. The global registry (which
            # the write path already updated) covers the legacy/no-project
            # resolution fallback; creating a registry here would wrongly
            # engage strict isolation for this conv.
            return None
        # Idempotent: same path already present → refresh access, return name.
        for rn, rs in conv_map.items():
            if os.path.abspath(rs['path']) == abs_path:
                rs['access'] = _access
                if view is not None:
                    _execution_root_view.set(_freeze_root_policy(conv_id, view[3]))
                return rn
        rname = name or os.path.basename(abs_path) or 'root'
        orig = rname
        counter = 2
        while rname.lower() in {label.lower() for label in conv_map}:
            rname = f'{orig}_{counter}'
            counter += 1
        conv_map[rname] = _make_root_state(abs_path, access=_access)
        if view is not None:
            _execution_root_view.set(_freeze_root_policy(conv_id, view[3]))
        # Eviction of a projection must not discard the acknowledged policy.
        if conv_id in _conv_roots:
            _conv_roots.move_to_end(conv_id)
    logger.info('[Config] add_conv_root conv=%s [%s] %s',
                conv_id[:12] if conv_id else '?', rname, abs_path)
    return rname


def _conv_has_live_task(conv_id):
    """True when *conv_id* currently owns a pending/running task.

    Used to PIN a live conversation's root registry against LRU eviction:
    evicting the registry of a conv whose task is mid-flight makes its next
    ``name:rel/path`` tool call resolve against the (concurrency-clobbered)
    global ``_roots`` — either raising ``UnknownWorkspaceRootError`` (caught
    by the tools.py self-heal) OR, if the global holds a colliding-basename
    root, SILENTLY misrouting the write to the wrong tree (the self-heal is
    exception-only and never sees this). Both are prevented by not evicting a
    live conv while an idle one is available.

    Reuses the SAME runtime-task signal the GET-path reconcile uses
    (manager._latest_task_for_conv + the chat TaskRuntime status), so
    "is this conv live?" has one definition across the codebase.

    Fails OPEN: if the probe cannot run (import cycle, runtime absent), it
    returns False so eviction is NEVER blocked — an unbounded _conv_roots is
    a worse failure than a rare mis-eviction, and the self-heal still covers
    the latter.
    """
    if not conv_id:
        return False
    try:
        from lib.tasks_pkg.manager.runtime import (
            _latest_task_for_conv,
            chat_task_runtime,
        )
        # Isolation uses a task ID as the registry namespace. Probe it first;
        # conversation keys retain the existing latest-task lookup fallback.
        t = chat_task_runtime.get(conv_id)
        if t is None:
            tid = _latest_task_for_conv(conv_id)
            if not tid:
                return False
            t = chat_task_runtime.get(tid)
        return bool(t and t.get('status') in ('pending', 'running'))
    except Exception as e:
        logger.debug('[Config] live-task probe failed for conv=%s: %s — '
                     'treating as idle (eviction not blocked)',
                     conv_id[:12] if conv_id else '?', e)
        return False


def _evict_conv_roots_over_cap():
    """Evict oldest IDLE conv registries until within ``MAX_CONV_ROOTS``.

    Called under ``_lock``. Walks _conv_roots oldest→newest and drops the
    first entry whose conv has NO live task (see ``_conv_has_live_task``),
    preserving the LIVE ones regardless of age. If every over-cap candidate
    from the oldest end is live (pathological: >MAX_CONV_ROOTS simultaneously
    running tasks), it falls back to evicting the strict oldest and logs a
    WARNING — bounding memory takes precedence, and the self-heal covers the
    displaced conv.
    """
    while len(_conv_roots) > MAX_CONV_ROOTS:
        # Find the oldest entry that is NOT protected by a live task.
        victim = None
        for cid in _conv_roots:  # OrderedDict iterates oldest→newest
            if not _conv_has_live_task(cid):
                victim = cid
                break
        if victim is None:
            # All entries are live — evict strict-oldest to bound memory.
            victim, _ = _conv_roots.popitem(last=False)
            _conv_primary.pop(victim, None)
            logger.warning('[Config] LRU cap %d exceeded but ALL conv roots '
                           'have live tasks — force-evicted oldest live conv '
                           '%s (self-heal will cover it if it resolves a '
                           'namespaced path)', MAX_CONV_ROOTS, victim[:12])
            continue
        _conv_roots.pop(victim, None)
        _conv_primary.pop(victim, None)
        logger.debug('[Config] LRU-evicted idle conv root state for %s '
                     '(over %d cap)', victim[:12], MAX_CONV_ROOTS)


def _cwd_within_conv_roots(conv_id, abs_cwd):
    """True when *abs_cwd* is inside (or equal to) a registered root for *conv_id*.

    Uses the SAME conv-scoped registry (with global-``_roots`` fallback) as the
    path resolver, so "is this cwd legitimately mine?" has one definition. This
    is the isolation gate for the sticky cwd: a directory outside every root the
    conversation owns is refused so app A's ``cd`` can never leak into app B.
    """
    if not abs_cwd:
        return False
    try:
        target = os.path.realpath(abs_cwd)
    except OSError as e:
        logger.debug('[Config] realpath of cwd failed, using fallback: %s', e)
        return False
    roots = list(get_conv_roots(conv_id).values())
    for rs in roots:
        try:
            rp = os.path.realpath(rs['path'])
        except OSError as e:
            logger.debug('[Config] realpath of root failed, using fallback: %s', e)
            continue
        if target == rp or target.startswith(rp + os.sep):
            return True
    return False


def get_conv_cwd(conv_id):
    """Return the sticky working directory for *conv_id*, or None.

    Only returns a path that (a) was previously accepted by ``set_conv_cwd``
    and (b) still exists as a directory. A vanished sticky (the model deleted
    or renamed the dir) safe-degrades to None so the caller falls back to the
    project-root anchor instead of raising at subprocess spawn.
    """
    if not conv_id:
        return None
    with _lock:
        cwd = _conv_cwd.get(conv_id)
        if cwd:
            _conv_cwd.move_to_end(conv_id)
    if cwd and os.path.isdir(cwd) and _cwd_within_conv_roots(conv_id, cwd):
        return cwd
    if cwd:
        # Stale (deleted/renamed) — drop it so we don't hand a dead cwd to Popen.
        with _lock:
            _conv_cwd.pop(conv_id, None)
        logger.debug('[Config] sticky cwd for conv=%s vanished (%s) — cleared',
                     conv_id[:12] if conv_id else '?', cwd)
    return None


def set_conv_cwd(conv_id, abs_cwd):
    """Remember *abs_cwd* as the sticky cwd for *conv_id* (isolation-gated).

    Refuses — leaving any existing anchor unchanged — when the target is not a
    directory or falls outside every root the conversation owns (see
    ``_cwd_within_conv_roots``). Returns True when the anchor was updated.
    """
    if not conv_id or not abs_cwd:
        return False
    abs_cwd = os.path.abspath(os.path.expanduser(abs_cwd))
    if not os.path.isdir(abs_cwd):
        return False
    if not _cwd_within_conv_roots(conv_id, abs_cwd):
        logger.debug('[Config] sticky cwd %s rejected for conv=%s '
                     '(outside registered roots)', abs_cwd,
                     conv_id[:12] if conv_id else '?')
        return False
    with _lock:
        if conv_id in _conv_cwd:
            _conv_cwd.move_to_end(conv_id)
        _conv_cwd[conv_id] = abs_cwd
        while len(_conv_cwd) > MAX_CONV_ROOTS:
            _conv_cwd.popitem(last=False)
    return True


def clear_conv_state(conv_id):
    """Drop a conversation's root registry (call on task/conv teardown)."""
    if not conv_id:
        return
    with _lock:
        _conv_roots.pop(conv_id, None)
        _conv_primary.pop(conv_id, None)
        _conv_cwd.pop(conv_id, None)


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
    roots = list(get_conv_roots(conv_id).values())
    return readonly_in_roots(target, roots)


def readonly_in_roots(abs_target, roots):
    """One deepest-root access decision for binding and every write boundary."""
    target = os.path.realpath(abs_target)
    candidates = [state for state in roots
                  if target == os.path.realpath(state['path']) or
                  target.startswith(os.path.realpath(state['path']).rstrip(os.sep) + os.sep)]
    best = max(candidates, key=lambda state: len(os.path.realpath(state['path'])), default=None)
    return best is not None and best.get('access') == 'ro'


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
    view = _scoped_root_policy(conv_id)
    if view is not None:
        if ':' in rel_path and not os.path.isabs(rel_path.split(':')[0]):
            name, _, rest = rel_path.partition(':')
            for label, state in view[2].items():
                if label.lower() == name.lower():
                    return state['path'], rest or '.'
            raise UnknownWorkspaceRootError(
                f'Unknown workspace root: {name} (available: {", ".join(view[2])})')
        if not view[1]:
            raise ValueError('No project path set for this task')
        return view[1], rel_path
    with _lock:
        if ':' in rel_path and not os.path.isabs(rel_path.split(':')[0]):

            if conv_id and conv_id not in _conv_roots:
                raise UnknownWorkspaceRootError('No roots registered for this conversation')
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
        # Fallback: primary (prefer conv-specific, else global).
        primary = None
        if conv_id:
            primary = _conv_primary.get(conv_id)
        else:
            primary = _state['path']
        if not primary:
            raise ValueError('No project path set')
        return primary, rel_path


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
        # Never serialize the raw undo log to clients. Each entry stores the
        #   full pre-image (originalContent) of every edited file, so a
        #   long-running project can balloon this response to tens of MB
        #   (Lighthouse flagged /api/v1/project/set at 28MB). The frontend only
        #   consumes modificationsCount; drop the heavy blobs from the payload.
        s.pop('modifications', None)
        # Always include extra roots so the frontend stays in sync
        extra = []
        primary = _state.get('path')
        for rn, rs in _roots.items():
            if rs['path'] != primary:
                extra.append({'path': rs['path'], 'name': rn,
                              'fileCount': rs['fileCount'],
                              'scanning': rs['scanning'],
                              'readOnly': rs.get('access') == 'ro'})
        s['extraRoots'] = extra
        # Primary root's own access flag (the primary may itself be RO).
        if primary:
            for rs in _roots.values():
                if rs['path'] == primary:
                    s['readOnly'] = rs.get('access') == 'ro'
                    break
        # Cross-DC latency indicator
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
        _attach_git_branches(s, primary)
        return s


def get_state_for_conv(conv_id):
    """Return project state scoped to *conv_id*'s OWN root registry.

    ``get_state()`` builds ``extraRoots`` from the PROCESS-GLOBAL ``_roots``,
    which every conversation shares. A background task's absolute-path write
    (or a prior conversation's extras) lands in that global registry, so
    ``get_state()`` would paint those roots onto whatever conversation happens
    to be reading ``/api/v1/project/status`` — the "extra paths appear on a
    conv that never had them" leak.

    This variant sources the primary + extras from the conversation's SCOPED
    registry (``_conv_roots`` / ``_conv_primary``) when it owns one, so a
    background task touching the global registry can never bleed onto another
    conv's bar. When the conv has no scoped registry (legacy / single-user UI
    path, or a conv the UI never wired a project to) it degrades to the global
    :func:`get_state` — byte-identical to today.

    Non-root fields (modificationsCount, crossDC) mirror ``get_state()``.
    """
    if not conv_id:
        return get_state()
    with _lock:
        conv_map = _conv_roots.get(conv_id)
        if not conv_map:
            # No scoped registry — fall through to the legacy global view.
            return get_state()
        primary = _conv_primary.get(conv_id)
        s = dict(_state)
        # The conv's own primary is authoritative for its bar — NOT the
        # global _state['path'] (which may be another conv's project).
        if primary:
            s['path'] = primary
        s['modificationsCount'] = len(_state.get('modifications', []))
        s.pop('modifications', None)
        extra = []
        for rn, rs in conv_map.items():
            if rs['path'] != primary:
                extra.append({'path': rs['path'], 'name': rn,
                              'fileCount': rs['fileCount'],
                              'scanning': rs['scanning'],
                              'readOnly': rs.get('access') == 'ro'})
        s['extraRoots'] = extra
        # Primary root's own access flag (the primary may itself be RO).
        if primary:
            for rs in conv_map.values():
                if rs['path'] == primary:
                    s['readOnly'] = rs.get('access') == 'ro'
                    break
    # Cross-DC latency indicator (outside _lock — cross_dc has its own cache).
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
        logger.debug('[Config] cross_dc info unavailable (conv=%s): %s',
                     conv_id[:12] if conv_id else '?', e)
    _attach_git_branches(s, primary)
    return s


def _attach_git_branches(state, primary):
    """Attach ``gitBranch`` / ``gitBranches`` to a project state payload.

    Probes the primary root (and each extra root) for its enclosing git work
    tree's checked-out branch so the frontend's per-turn context rail can
    show — and live-update — the branch a turn's writes actually land on.
    Best-effort: a path outside any repo, a detached HEAD, or a missing git
    binary simply yields an empty branch string, never an exception. Mirrors
    the {path, branch} pairs under ``gitBranches`` (primary first) so a
    multi-root rail can label each chip with its own branch.
    """
    try:
        from lib.project_mod.scanner import git_current_branch
    except Exception as e:  # pragma: no cover - import-time cycle guard
        logger.debug('[Config] git branch probe unavailable: %s', e)
        return state
    branches = []
    seen = set()
    paths = ([primary] if primary else []) + [
        r.get('path') for r in (state.get('extraRoots') or []) if r.get('path')
    ]
    for path in paths:
        if not path or path in seen:
            continue
        seen.add(path)
        branch = git_current_branch(path)
        if branch:
            branches.append({'path': path, 'branch': branch})
    if branches:
        state['gitBranch'] = branches[0]['branch']
        state['gitBranches'] = branches
    return state


def get_project_path():
    with _lock:
        return _state['path']


# ═══════════════════════════════════════════════════════
#  Recent Projects (server-side persistence)
# ═══════════════════════════════════════════════════════

def get_recent_projects(*, user_id: int):
    """Return list of recent projects sorted by last_used desc.

    No LIMIT — callers (frontend) decide how many to display. Keeping
    the full list server-side ensures a newly-added project never gets
    hidden by an artificial window size.
    """
    from lib.project_mod.recent_repository import RecentProjectRepository

    return RecentProjectRepository(user_id).list()


def save_recent_project(path, *, user_id: int):
    """Insert or update a recent project entry."""
    from lib.project_mod.recent_repository import RecentProjectRepository

    RecentProjectRepository(user_id).touch(path)


def save_recent_projects(paths, *, user_id: int):
    """Touch a bounded project-path batch in one owner-scoped transaction."""
    from lib.project_mod.recent_repository import RecentProjectRepository

    return RecentProjectRepository(user_id).touch_many(paths)


def clear_recent_projects(*, user_id: int):
    """Delete all recent project entries."""
    from lib.project_mod.recent_repository import RecentProjectRepository

    RecentProjectRepository(user_id).clear()



def relink_project_path(old_path, new_path, *, user_id: int):
    """Re-key recent, conversation, and Project Brain state after a move."""
    from lib.project_mod.recent_repository import RecentProjectRepository

    return RecentProjectRepository(user_id).relink(old_path, new_path)

# ═══════════════════════════════════════════════════════
#  Modification History (后悔药 / Undo)
