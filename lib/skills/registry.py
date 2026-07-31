"""lib/skills/registry.py — Installed skill-package enumeration.

A *skill package* is a user-installed instruction bundle (Anthropic
AgentSkills / OpenClaw format): a directory holding a ``SKILL.md`` with
YAML frontmatter plus optional ``references/`` / ``scripts/`` / ``assets/``
sub-files. Skills are a DIFFERENT NOUN from memories:

  * memories are MODEL-authored experience notes (flat ``*.md``), discovered
    by BM25 search / prefetch;
  * skills are USER-installed capability packs, discovered by the
    always-visible ``<available_skills>`` system-prompt index and activated
    on demand (see ``lib/skills/injection.py`` + ``lib/skills/activate.py``).

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

__all__ = ['list_skills', 'get_skill', 'uninstall_skill']


def list_skills(project_path: str | None = None,
                extra_paths: list[str] | None = None) -> list[dict]:
    """List every installed skill package across the global store + roots.

    De-duplicated by id with the server-side global store winning, then the
    primary root, then each extra root — the SAME collision order
    ``list_all_memories`` uses, so the transitional union in
    ``list_all_memories`` stays byte-identical.

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

        for mem in _list_skill_packages_in_dir(
                _server_global_skills_dir(), scope='global'):
            if mem['id'] in seen_ids:
                continue
            seen_ids.add(mem['id'])
            skills.append(mem)

        for root in roots:
            proj_dir = os.path.join(root, PROJECT_SKILLS_SUBDIR)
            for mem in _list_skill_packages_in_dir(proj_dir, scope='project'):
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
    logger.info('[Skills] uninstalled skill package %s (%s)', skill_id, pkg)
    return True
