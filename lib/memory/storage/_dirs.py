"""lib/memory/storage/_dirs.py — Constants, path resolution, per-root globals
migration + the SHARED mutable process state.

Holds the module-level ``_lock`` (Lock) and ``_migrated_roots`` (set tracking
which roots have had their legacy global dir migrated). These MUST live in a
single submodule and be shared BY REFERENCE — importers bind the same objects,
so mutation from :func:`_migrate_one_root_globals` / :func:`list_all_memories`
is globally visible. Also owns the ``_FM_RE``-adjacent path constants and
``_PROJECT_ROOT``.
"""

import os
import shutil
import threading

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════

# Legacy per-project global location (still READ for back-compat + migrated).
GLOBAL_MEMORY_SUBDIR = os.path.join('.tofu', 'skills', 'global')
PROJECT_MEMORY_SUBDIR = os.path.join('.tofu', 'skills')
MIN_DESCRIPTION_LENGTH = 20

# Keep GLOBAL_MEMORY_DIR as a computed property for backward compat
# (injection.py references it for the path template)
GLOBAL_MEMORY_DIR = None  # Set dynamically; see _get_global_memory_dir()

# Project root = three levels up from lib/memory/storage/_dirs.py (mirrors the
# BASE_DIR computation in lib/config_dir.py + lib/database.py).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Server-side global memory store, relative to the resolved data dir.
SERVER_GLOBAL_MEMORY_SUBPATH = os.path.join('memories', 'global')

_lock = threading.Lock()

# Roots whose legacy ``.tofu/skills/global`` dir has already been migrated
# into the server store this process (idempotent — guards repeated scans).
_migrated_roots: set = set()


# ═══════════════════════════════════════════════════════
#  Directory helpers
# ═══════════════════════════════════════════════════════

def _ensure_dir(dirpath):
    """Create directory if it doesn't exist."""
    os.makedirs(dirpath, exist_ok=True)


def _server_data_dir():
    """Return the server data directory — the SAME writable root the DB and
    logs use.

    Delegates to ``lib.runtime_paths.data_root()`` so the global memory store
    always co-locates with ``data/tofu.db`` under one resolved base. This
    matters once a source checkout defaults its data root OUT of the code tree
    (``TOFU_DATA_LAYOUT``): recomputing ``<project_root>/data`` here would split
    global memories back into the tree while the DB moved to the per-user dir —
    silently orphaning them. Falls back to the legacy in-tree path only if the
    import fails (should never happen; runtime_paths is dependency-free).
    """
    try:
        from lib.runtime_paths import data_root
        return data_root()
    except Exception as e:  # pragma: no cover — defensive
        logger.warning('[Memory] runtime_paths.data_root() unavailable, '
                       'falling back to in-tree data/: %s', e)
        return os.environ.get('TOFU_DATA_DIR') or os.path.join(_PROJECT_ROOT, 'data')


def _server_global_memory_dir():
    """Return the canonical server-side global memory directory.

    ``<data>/memories/global/``.  Independent of any project, so global
    memories work in a project-less chat and are shared across projects.
    Lives under ``data/`` which export.py already excludes wholesale.
    """
    return os.path.join(_server_data_dir(), SERVER_GLOBAL_MEMORY_SUBPATH)


def _get_global_memory_dir(project_path):
    """Return the LEGACY per-project global memory directory.

    Legacy global memories lived at ``<project>/.tofu/skills/global/``.
    Still read for back-compat (and migrated into the server store); returns
    ``None`` when no project root is set.
    """
    if not project_path:
        return None
    return os.path.join(project_path, GLOBAL_MEMORY_SUBDIR)


def resolve_target_dir(scope, project_path):
    """Return the on-disk directory where a memory of ``scope`` should live.

    Used by both :func:`create_memory` and the package installer.

    * ``scope='global'`` → the server-side store (``<data>/memories/global/``),
      created on demand. No project root required — global memories are
      project-independent.
    * ``scope='project'`` → ``<project>/.tofu/skills/``. Raises ``ValueError``
      when ``project_path`` is missing (a project-scoped memory has nowhere
      to live without one).
    """
    if scope == 'global':
        target = _server_global_memory_dir()
        _ensure_dir(target)
        return target
    if not project_path:
        raise ValueError('project_path required for project-scoped memory storage')
    return os.path.join(project_path, PROJECT_MEMORY_SUBDIR)


def _iter_roots(project_path, extra_paths):
    """Return the ordered, de-duplicated list of roots to scan.

    The primary ``project_path`` always comes first (so it wins on an id
    collision); any ``extra_paths`` follow in order, skipping blanks and
    duplicates.
    """
    roots = []
    if project_path:
        roots.append(project_path)
    for p in (extra_paths or []):
        if p and p not in roots:
            roots.append(p)
    return roots


def _migrate_one_root_globals(root):
    """Copy a root's legacy global memories into the server store.

    Idempotent: a legacy entry is copied only when no server-store entry of
    the same id already exists. Files are copied (not moved) so the legacy
    original survives the transition window — it is shadowed by id de-dup
    on read (server store wins). Handles both flat ``<id>.md`` files and
    ``<id>/SKILL.md`` skill packages.
    """
    legacy_dir = _get_global_memory_dir(root)
    if not legacy_dir or not os.path.isdir(legacy_dir):
        return
    server_dir = _server_global_memory_dir()
    _ensure_dir(server_dir)
    for entry in sorted(os.listdir(legacy_dir)):
        if entry.startswith('.'):
            continue
        src = os.path.join(legacy_dir, entry)
        if os.path.isfile(src) and entry.endswith('.md'):
            dst = os.path.join(server_dir, entry)
            if os.path.exists(dst) or os.path.isdir(os.path.splitext(dst)[0]):
                continue
            shutil.copy2(src, dst)
            logger.info('[Memory] migrated legacy global memory %s → %s',
                        entry, server_dir)
        elif os.path.isdir(src) and os.path.isfile(
                os.path.join(src, 'SKILL.md')):
            dst = os.path.join(server_dir, entry)
            if os.path.exists(dst):
                continue
            shutil.copytree(src, dst)
            logger.info('[Memory] migrated legacy global skill package %s → %s',
                        entry, server_dir)
