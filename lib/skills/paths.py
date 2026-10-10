"""Owner-aware filesystem locations for installed skill packages.

This module is the only skills-layer seam that maps a logical scope to a
filesystem directory. Project packages inherit the attached project's
authority. Global packages are namespaced by explicit owner id; calls without
an owner retain the pre-owner path only for offline tools and compatibility
tests.
"""

from __future__ import annotations

import os

from lib.identity import PERSONAL_USER_ID, require_user_id
from lib.memory.storage import PROJECT_SKILLS_SUBDIR
from lib.memory.storage import _dirs as memory_dirs

OWNER_GLOBAL_SKILLS_SUBPATH = os.path.join('skills', 'users')


def extra_skill_project_paths(project_paths: object, primary: str | None) -> list[str]:
    """Read attached workspace roots from current records or legacy strings.

    Callers pass no roots when project access is disabled. Access mode stays
    in task configuration; reading a guide never upgrades a read-only root.
    Invalid entries are ignored rather than stringified into host paths.
    """
    if not isinstance(project_paths, (list, tuple)):
        return []
    roots = []
    seen = {primary}
    for entry in project_paths:
        path = entry.get('path') if isinstance(entry, dict) else entry
        if isinstance(path, str) and path.strip() and path not in seen:
            roots.append(path)
            seen.add(path)
    return roots


def owner_global_skills_dir(owner_user_id: int) -> str:
    """Return ``<data>/skills/users/<owner>/`` for one explicit owner."""
    owner = require_user_id(owner_user_id, context='global skill storage')
    return os.path.join(
        memory_dirs._server_data_dir(), OWNER_GLOBAL_SKILLS_SUBPATH, str(owner))


def resolve_skill_install_dir(
    scope: str,
    project_path: str | None,
    *,
    owner_user_id: int | None,
) -> str:
    """Resolve an install target without inventing a process-global owner.

    ``owner_user_id=None`` deliberately selects the legacy global path. It is
    retained for offline migration utilities and backwards-compatible direct
    library callers; authenticated routes and task handlers always pass an
    owner.
    """
    if scope == 'global':
        target = (
            memory_dirs._server_global_skills_dir()
            if owner_user_id is None
            else owner_global_skills_dir(owner_user_id)
        )
        os.makedirs(target, exist_ok=True)
        return target
    if scope != 'project':
        raise ValueError(f'Invalid skill scope: {scope!r}')
    if not project_path:
        raise ValueError(
            'project_path required for project-scoped skill storage')
    return os.path.join(project_path, PROJECT_SKILLS_SUBDIR)


def skill_scan_dirs(
    project_path: str | None,
    extra_paths: list[str] | None,
    *,
    owner_user_id: int | None,
) -> list[tuple[str, str]]:
    """Return precedence-ordered ``(directory, scope)`` scan roots.

    The new owner-global directory wins. The old process-global store is only
    visible to the declared personal owner, preventing legacy data from
    becoming an accidental multi-user sharing mechanism.
    """
    roots: list[str] = []
    for path in [project_path, *(extra_paths or [])]:
        if path and path not in roots:
            roots.append(path)
    out = []
    for root in roots:
        for subdir in (PROJECT_SKILLS_SUBDIR, '.agents/skills', '.codex/skills'):
            directory = os.path.join(root, subdir)
            # A project grants access to its own packages, never a symlinked
            # host-home catalog belonging to an unrelated owner.
            if os.path.commonpath((os.path.realpath(root),
                                   os.path.realpath(directory))) == os.path.realpath(root):
                out.append((directory, 'project'))
    if owner_user_id is None:
        out.append((memory_dirs._server_global_skills_dir(), 'global'))
        return out

    owner = require_user_id(owner_user_id, context='skill registry')
    out.append((owner_global_skills_dir(owner), 'global'))
    if owner == PERSONAL_USER_ID:
        legacy = memory_dirs._server_global_skills_dir()
        if legacy != out[-1][0]:
            out.append((legacy, 'global'))
    return out


def legacy_global_skills_dir() -> str:
    """Compatibility location used only by migration-aware callers."""
    return memory_dirs._server_global_skills_dir()


__all__ = [
    'OWNER_GLOBAL_SKILLS_SUBPATH',
    'extra_skill_project_paths',
    'legacy_global_skills_dir',
    'owner_global_skills_dir',
    'resolve_skill_install_dir',
    'skill_scan_dirs',
]
