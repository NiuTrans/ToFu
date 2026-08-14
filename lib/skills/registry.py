"""lib/skills/registry.py — Installed skill-package enumeration.

A *skill package* is a user-installed instruction bundle (Anthropic
AgentSkills / OpenClaw format): a directory holding a ``SKILL.md`` with
YAML frontmatter plus optional ``references/`` / ``scripts/`` / ``assets/``
sub-files. Skills are a DIFFERENT NOUN from memories:

  * memories are MODEL-authored experience notes (flat ``*.md``), discovered
    by BM25 search / prefetch;
  * skills are USER-installed capability packs, discovered by the
    always-visible ``<available_skills>`` index and loaded for the current task
    on demand (see ``lib/skills/injection.py`` + ``lib/skills/load.py``).

Physical homes (post-split, see ``lib/memory/storage/_dirs.py``):

  * project scope → ``<project>/.tofu/skills/<id>/``
  * global scope  → ``<data>/skills/global/<id>/``

This module reuses the memory storage layer's file helpers
(``_memory_from_file`` gives the frontmatter parse + eligibility gating +
``.catalog_id`` marker handling) so a skill dict has the same shape the
Settings UI already renders (``eligible`` / ``ineligible_reasons`` /
``is_package`` / ``package_dir`` / ``catalog_id`` …).
"""

from __future__ import annotations

import os

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['list_skills', 'get_skill', 'uninstall_skill',
           'set_skill_scope']


def list_skills(project_path: str | None = None,
                extra_paths: list[str] | None = None) -> list[dict]:
    """List every installed skill package across the global store + roots.

    De-duplicated by id with the primary project winning, then each extra
    workspace root, then the server-side global store. A project-local skill
    can therefore intentionally override a general global workflow.

    Returns a list of skill dicts (memory-shaped, ``is_package=True``).
    """
    from lib.memory.storage import (
        PROJECT_SKILLS_SUBDIR,
        _iter_roots,
        _list_skill_packages_in_dir,
        _lock,
        _server_global_skills_dir,
        run_storage_migrations,
    )

    roots = _iter_roots(project_path, extra_paths)
    skills: list[dict] = []
    seen_ids: set = set()
    with _lock:
        # Ensure the post-split layout before scanning (idempotent).
        run_storage_migrations(project_path, extra_paths)

        for root in roots:
            proj_dir = os.path.join(root, PROJECT_SKILLS_SUBDIR)
            for mem in _list_skill_packages_in_dir(proj_dir, scope='project'):
                if mem['id'] in seen_ids:
                    continue
                seen_ids.add(mem['id'])
                skills.append(mem)
        for mem in _list_skill_packages_in_dir(
                _server_global_skills_dir(), scope='global'):
            if mem['id'] in seen_ids:
                continue
            seen_ids.add(mem['id'])
            skills.append(mem)
    return skills


def get_skill(skill_id: str,
              project_path: str | None = None,
              extra_paths: list[str] | None = None) -> dict | None:
    """Get one installed skill package by id. Returns the dict or None."""
    for s in list_skills(project_path, extra_paths=extra_paths):
        if s['id'] == skill_id:
            return s
    return None


def uninstall_skill(skill_id: str,
                    project_path: str | None = None,
                    extra_paths: list[str] | None = None) -> bool:
    """Uninstall a skill package (remove its directory). USER action.

    This is the Skills-tab uninstall path — deliberately NOT routed
    through ``lib.memory.storage.delete_memory`` (which is model-CRUD
    guarded and refuses packages). Path-safety: the package dir must
    live inside a known skills tree (a root's ``.tofu/skills/`` or the
    server-side global skills store).

    Returns True if the package was removed.
    """
    import shutil

    from lib.memory.storage import (
        PROJECT_SKILLS_SUBDIR,
        _iter_roots,
        _server_global_skills_dir,
    )

    skill = get_skill(skill_id, project_path, extra_paths=extra_paths)
    if not skill:
        return False
    pkg = skill.get('package_dir')
    if not pkg or not os.path.isdir(pkg):
        logger.warning('[Skills] uninstall: package dir missing for %s (%r)',
                       skill_id, pkg)
        return False

    allowed = [os.path.realpath(_server_global_skills_dir())]
    allowed += [
        os.path.realpath(os.path.join(r, PROJECT_SKILLS_SUBDIR))
        for r in _iter_roots(project_path, extra_paths)
    ]
    pkg_real = os.path.realpath(pkg)
    if not any(pkg_real.startswith(a + os.sep) or pkg_real == a
               for a in allowed):
        logger.warning('[Skills] uninstall refused — outside skills trees: '
                       '%s', pkg)
        return False

    shutil.rmtree(pkg)
    # No orphan secrets: the skill's vault bindings go with it.
    try:
        from lib.skills.env import clear_skill_env
        clear_skill_env(skill_id)
    except Exception as e:
        logger.warning('[Skills] vault cleanup for %s failed: %s',
                       skill_id, e)
    logger.info('[Skills] uninstalled skill package %s (%s)', skill_id, pkg)
    return True


def set_skill_scope(skill_id: str, scope: str,
                    project_path: str | None = None,
                    extra_paths: list[str] | None = None) -> dict | None:
    """Move an installed skill package between project and global scope.

    Skill packages are external capability packs — the right home for most
    is the GLOBAL store so they work in project-less chat too; project
    scope is for packs that only make sense inside one workspace. The vault
    bindings (``skill.<id>.*``) are scope-independent and need no move.

    Returns the updated skill dict, or None when the skill was not found.
    Raises ValueError on an invalid scope or a destination collision.
    """
    import shutil

    from lib.memory.storage import (
        _memory_from_file,
        resolve_skills_dir,
    )

    if scope not in ('project', 'global'):
        raise ValueError(f'Invalid scope: {scope!r}')
    skill = get_skill(skill_id, project_path, extra_paths=extra_paths)
    if not skill:
        return None
    if skill.get('scope') == scope:
        return skill

    src = skill.get('package_dir')
    if not src or not os.path.isdir(src):
        raise ValueError(f'package dir missing for {skill_id}')

    dst_root = resolve_skills_dir(scope, project_path)
    os.makedirs(dst_root, exist_ok=True)
    dst = os.path.join(dst_root, skill_id)
    if os.path.exists(dst):
        raise ValueError(f'{skill_id} already exists in {scope} scope')

    shutil.move(src, dst)
    logger.info('[Skills] moved %s: %s → %s scope', skill_id,
                skill.get('scope'), scope)
    return _memory_from_file(
        os.path.join(dst, 'SKILL.md'), scope=scope,
        package_dir=dst, memory_id_override=skill_id)
