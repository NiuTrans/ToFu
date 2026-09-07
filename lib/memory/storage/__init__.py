"""lib/memory/storage — File I/O, YAML frontmatter, CRUD operations.

Memories are plain Markdown files stored in:
  • Global:  <data>/memories/global/*.md  — server-side store, shared across
             ALL projects and reachable even with no project attached.
             ``<data>`` is ``$TOFU_DATA_DIR`` when set, else ``<root>/data``.
  • Project: <project>/.tofu/memories/*.md — travels with the project
             tree (pre-split flat files at ``.tofu/skills/*.md`` are moved
             here by an idempotent migration on first read).

SKILL PACKAGES (``<dir>/<id>/SKILL.md``) are a different noun: they live at
``<project>/.tofu/skills/<id>/`` + ``<data>/skills/global/<id>/`` and are
enumerated by ``lib/skills/registry`` — this module only hosts their shared
file/frontmatter helpers.

The global store moved out of ``<project>/.tofu/skills/global/`` (2026-06):
rooting a "global" memory under one project meant it was invisible from every
other project and impossible to reach in a project-less chat. The legacy
per-project ``global/`` directory is still READ (back-compat) and its contents
are copied into the server store once per root (idempotent migration), so no
existing global memory is lost.

Multi-root: the read/list functions accept an optional ``extra_paths`` list
(the non-primary workspace roots of a multi-root session). Project memories are
UNIONED across the primary root + every extra root and de-duplicated by id
(server-global store wins, then primary root). NEW project memories are written
ONLY to the primary ``project_path``; global memories always go to the server
store; update/delete/merge locate a memory in whichever root it lives and
mutate it in place.

────────────────────────────────────────────────────────────────────────
This module was split into a package (``storage/``) for maintainability. The
import path is UNCHANGED (``from lib.memory.storage import X`` still works) —
this ``__init__`` re-exports every public + private symbol from the internal
submodules so the facade is byte-identical to the former single-file module.
Submodules:
  • ``_frontmatter`` — frontmatter parse/build + str-list/metadata helpers
  • ``_dirs``        — constants, path resolution, per-root globals migration
                       and the SHARED ``_lock`` / ``_migrated_roots`` state
  • ``_files``       — per-file read/write, eligibility, dir listing, id gen
  • ``_crud``        — list / query / create / update / delete / merge / toggle
"""

from lib.log import get_logger

logger = get_logger(__name__)

# ── Constants + shared state + path helpers (from _dirs) ─────────────
from ._dirs import (
    GLOBAL_MEMORY_DIR,
    GLOBAL_MEMORY_SUBDIR,
    LEGACY_PROJECT_MEMORY_SUBDIR,
    PROJECT_MEMORY_SUBDIR,
    PROJECT_SKILLS_SUBDIR,
    MIN_DESCRIPTION_LENGTH,
    SERVER_GLOBAL_MEMORY_SUBPATH,
    SERVER_GLOBAL_SKILLS_SUBPATH,
    _PROJECT_ROOT,
    _lock,
    _migrated_roots,
    _ensure_dir,
    _server_data_dir,
    _server_global_memory_dir,
    _server_global_skills_dir,
    _get_global_memory_dir,
    resolve_target_dir,
    resolve_skills_dir,
    run_storage_migrations,
    _iter_roots,
    _migrate_one_root_globals,
    _migrate_project_flat_memories,
    _migrate_server_store_packages,
)

# ── Frontmatter parsing / building (from _frontmatter) ───────────────
from ._frontmatter import (
    _FM_RE,
    _parse_frontmatter,
    _coerce_str_list,
    _extract_package_metadata,
    _build_frontmatter,
)

# ── Per-file I/O + eligibility (from _files) ─────────────────────────
from ._files import (
    _check_memory_eligible,
    _memory_from_file,
    _write_memory_file,
    _list_memories_in_dir,
    _list_skill_packages_in_dir,
    _make_memory_id,
)

# ── CRUD / list / query (from _crud) ─────────────────────────────────
from ._crud import (
    MemoryRevisionConflict,
    list_all_memories,
    list_memories,
    get_memory,
    get_enabled_memories,
    get_eligible_memories,
    iter_eligible_memories,
    load_eligible_memories,
    create_memory,
    clear_memories,
    update_memory,
    delete_memory,
    merge_memories,
    toggle_memory,
)

# Historical exports stay intact; bounded iterator/hydration repository seams
# are additive. ``lib/memory/__init__.py`` controls its own public surface.
__all__ = [
    'GLOBAL_MEMORY_DIR', 'GLOBAL_MEMORY_SUBDIR', 'PROJECT_MEMORY_SUBDIR', 'MIN_DESCRIPTION_LENGTH',
    'SERVER_GLOBAL_MEMORY_SUBPATH',
    'LEGACY_PROJECT_MEMORY_SUBDIR', 'PROJECT_SKILLS_SUBDIR', 'SERVER_GLOBAL_SKILLS_SUBPATH',
    'list_all_memories', 'list_memories', 'get_memory', 'get_enabled_memories',
    'get_eligible_memories',
    'iter_eligible_memories', 'load_eligible_memories',
    'MemoryRevisionConflict',
    'create_memory', 'update_memory', 'delete_memory', 'clear_memories', 'merge_memories',
    'toggle_memory',
    'resolve_target_dir', 'resolve_skills_dir', 'run_storage_migrations',
]
