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
  * global scope  → ``<data>/skills/users/<owner>/<id>/``

The pre-owner ``<data>/skills/global/`` tree remains a read-compatible source
only for the declared personal owner. Every authenticated caller passes an
explicit ``owner_user_id``; ``None`` is reserved for offline compatibility.

This module reuses the memory storage layer's file helpers
(``_memory_from_file`` gives the frontmatter parse + eligibility gating +
``.catalog_id`` marker handling) so a skill dict has the same shape the
Settings UI already renders (``eligible`` / ``ineligible_reasons`` /
``is_package`` / ``package_dir`` / ``catalog_id`` …).
"""

from __future__ import annotations

import os
import threading

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['list_skills', 'get_skill', 'uninstall_skill',
           'set_skill_enabled', 'set_skill_scope']


_skills_cache: dict = {}
_skills_cache_lock = threading.Lock()
_SKILLS_CACHE_MAX = 128
_SKILL_ROOT_MAX_PACKAGES = 2_000
_SKILL_ROOT_MAX_SCANNED_ENTRIES = 4_096


def _skill_scan_dirs(project_path, extra_paths, owner_user_id=None):
    from lib.skills.paths import skill_scan_dirs
    return skill_scan_dirs(
        project_path, extra_paths, owner_user_id=owner_user_id)


def _skill_package_entries(dirpath: str, scope: str) -> list[tuple[str, str]]:
    """Return a bounded, deterministic ``(id, SKILL.md)`` root snapshot."""
    packages: list[tuple[str, str]] = []
    if not os.path.isdir(dirpath):
        return packages
    scanned = 0
    try:
        iterator = os.scandir(dirpath)
    except OSError as exc:
        logger.warning('[Skills] cannot scan skill root %s: %s', dirpath, exc)
        return packages
    with iterator:
        for entry in iterator:
            scanned += 1
            if scanned > _SKILL_ROOT_MAX_SCANNED_ENTRIES:
                logger.warning('[Skills] skill root scan capped at %d entries: %s',
                               _SKILL_ROOT_MAX_SCANNED_ENTRIES, dirpath)
                break
            if entry.name.startswith('.'):
                continue
            if scope == 'project' and entry.name == 'global':
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            skill_md = os.path.join(entry.path, 'SKILL.md')
            if os.path.islink(skill_md) or not os.path.isfile(skill_md):
                continue
            packages.append((entry.name, skill_md))
            if len(packages) >= _SKILL_ROOT_MAX_PACKAGES:
                logger.warning('[Skills] skill root package cap reached: %s',
                               dirpath)
                break
    return sorted(packages, key=lambda row: row[0])


def _skill_scan_fingerprint(dirs):
    """Cheap mtime fingerprint of the skill dirs (no SKILL.md reads)."""
    fp = []
    for dirpath, scope in dirs:
        entries = []
        for entry, skill_md in _skill_package_entries(dirpath, scope):
            try:
                mtime = os.stat(skill_md).st_mtime_ns
            except OSError:
                mtime = -1
            entries.append((entry, mtime))
        fp.append((dirpath, scope, tuple(entries)))
    return tuple(fp)


def _refresh_eligibility(skills, owner_user_id=None):
    """Re-derive eligibility on cached dicts (env/vault can change w/o mtime)."""
    from lib.memory.storage._files import _check_memory_eligible
    out = []
    for mem in skills:
        mem = dict(mem)
        eligible, reasons = _check_memory_eligible(
            mem, owner_user_id=owner_user_id)
        mem['eligible'] = eligible
        mem['ineligible_reasons'] = reasons
        out.append(mem)
    return out


def _invalidate_skills_cache():
    with _skills_cache_lock:
        _skills_cache.clear()


def list_skills(project_path: str | None = None,
                extra_paths: list[str] | None = None,
                owner_user_id: int | None = None) -> list[dict]:
    """List every installed skill package across the global store + roots.

    De-duplicated by id with the primary project winning, then each extra
    workspace root, then the server-side global store. A project-local skill
    can therefore intentionally override a general global workflow.

    Returns a list of skill dicts (memory-shaped, ``is_package=True``).
    """
    from lib.memory.storage import (
        _memory_from_file,
        _lock,
        run_storage_migrations,
    )

    dirs = _skill_scan_dirs(project_path, extra_paths, owner_user_id)
    cache_key = tuple(dirs)
    fp = _skill_scan_fingerprint(dirs)
    with _skills_cache_lock:
        cached = _skills_cache.get(cache_key)
    if cached is not None and cached[0] == fp:
        return _refresh_eligibility(cached[1], owner_user_id)

    skills: list[dict] = []
    seen_ids: set = set()
    with _lock:
        # Ensure the post-split layout before scanning (idempotent).
        run_storage_migrations(project_path, extra_paths)

        for directory, scope in dirs:
            for entry, skill_md in _skill_package_entries(directory, scope):
                mem = _memory_from_file(
                    skill_md, scope=scope,
                    package_dir=os.path.dirname(skill_md),
                    memory_id_override=entry,
                    owner_user_id=owner_user_id)
                if not mem:
                    continue
                if mem['id'] in seen_ids:
                    continue
                seen_ids.add(mem['id'])
                skills.append(mem)

    # Recompute the fingerprint AFTER migrations so the stored identity
    # matches the post-migration directory state.
    fp = _skill_scan_fingerprint(dirs)
    with _skills_cache_lock:
        if len(_skills_cache) >= _SKILLS_CACHE_MAX:
            _skills_cache.clear()
        _skills_cache[cache_key] = (fp, skills)
    return _refresh_eligibility(skills, owner_user_id)


def get_skill(skill_id: str,
              project_path: str | None = None,
              extra_paths: list[str] | None = None,
              owner_user_id: int | None = None) -> dict | None:
    """Get one installed skill package by id. Returns the dict or None."""
    for s in list_skills(
            project_path, extra_paths=extra_paths,
            owner_user_id=owner_user_id):
        if s['id'] == skill_id:
            return s
    return None


def uninstall_skill(skill_id: str,
                    project_path: str | None = None,
                    extra_paths: list[str] | None = None,
                    owner_user_id: int | None = None) -> bool:
    """Uninstall a skill package (remove its directory). USER action.

    This is the Skills-tab uninstall path — deliberately NOT routed
    through ``lib.memory.storage.delete_memory`` (which is model-CRUD
    guarded and refuses packages). Path-safety: the package dir must
    live inside a known skills tree (a root's ``.tofu/skills/`` or the
    server-side global skills store).

    Returns True if the package was removed.
    """
    import shutil

    skill = get_skill(
        skill_id, project_path, extra_paths=extra_paths,
        owner_user_id=owner_user_id)
    if not skill:
        return False
    pkg = skill.get('package_dir')
    if not pkg or not os.path.isdir(pkg):
        logger.warning('[Skills] uninstall: package dir missing for %s (%r)',
                       skill_id, pkg)
        return False

    allowed = [
        os.path.realpath(directory)
        for directory, _scope in _skill_scan_dirs(
            project_path, extra_paths, owner_user_id)
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
        clear_skill_env(skill_id, owner_user_id=owner_user_id)
    except Exception as e:
        logger.warning('[Skills] vault cleanup for %s failed: %s',
                       skill_id, e)
    _invalidate_skills_cache()
    logger.info('[Skills] uninstalled skill package %s (%s)', skill_id, pkg)
    return True


def set_skill_scope(skill_id: str, scope: str,
                    project_path: str | None = None,
                    extra_paths: list[str] | None = None,
                    owner_user_id: int | None = None) -> dict | None:
    """Move an installed skill package between project and global scope.

    Skill packages are external capability packs — the right home for most
    is the GLOBAL store so they work in project-less chat too; project
    scope is for packs that only make sense inside one workspace. The vault
    bindings (``skill.<id>.*``) are scope-independent and need no move.

    Returns the updated skill dict, or None when the skill was not found.
    Raises ValueError on an invalid scope or a destination collision.
    """
    import shutil

    from lib.memory.storage import _memory_from_file
    from lib.skills.paths import resolve_skill_install_dir

    if scope not in ('project', 'global'):
        raise ValueError(f'Invalid scope: {scope!r}')
    skill = get_skill(
        skill_id, project_path, extra_paths=extra_paths,
        owner_user_id=owner_user_id)
    if not skill:
        return None
    if skill.get('scope') == scope:
        return skill

    src = skill.get('package_dir')
    if not src or not os.path.isdir(src):
        raise ValueError(f'package dir missing for {skill_id}')

    dst_root = resolve_skill_install_dir(
        scope, project_path, owner_user_id=owner_user_id)
    os.makedirs(dst_root, exist_ok=True)
    dst = os.path.join(dst_root, skill_id)
    if os.path.exists(dst):
        raise ValueError(f'{skill_id} already exists in {scope} scope')

    shutil.move(src, dst)
    _invalidate_skills_cache()
    logger.info('[Skills] moved %s: %s → %s scope', skill_id,
                skill.get('scope'), scope)
    updated = _memory_from_file(
        os.path.join(dst, 'SKILL.md'), scope=scope,
        package_dir=dst, memory_id_override=skill_id)
    return _refresh_eligibility([updated], owner_user_id)[0] if updated else None


def set_skill_enabled(
    skill_id: str,
    enabled: bool | None = None,
    project_path: str | None = None,
    extra_paths: list[str] | None = None,
    owner_user_id: int | None = None,
) -> dict | None:
    """Persist a user-driven enable toggle for one installed skill.

    This stays in the skills repository instead of routing owner-global
    packages through the legacy memory union, which has no owner parameter.
    """
    from lib.json_store import locked_path, write_text_atomic
    from lib.memory.storage import _memory_from_file

    skill = get_skill(
        skill_id, project_path, extra_paths=extra_paths,
        owner_user_id=owner_user_id)
    if not skill:
        return None
    next_enabled = (
        not skill.get('enabled', True) if enabled is None else bool(enabled))

    # Preserve the third-party package byte-for-byte except for the one local
    # top-level flag. The generic memory writer intentionally emits only the
    # memory schema and would discard nested AgentSkills/OpenClaw metadata.
    filepath = skill['filepath']
    with locked_path(filepath):
        try:
            if os.path.getsize(filepath) > 2 * 1024 * 1024:
                raise ValueError('SKILL.md exceeds the 2 MiB instruction limit')
            with open(filepath, encoding='utf-8') as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f'cannot read SKILL.md for {skill_id}') from exc
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].strip() != '---':
            raise ValueError(f'SKILL.md for {skill_id} has no frontmatter')
        closing = next((
            index for index, line in enumerate(lines[1:], start=1)
            if line.strip() == '---' and not line.startswith((' ', '\t'))
        ), None)
        if closing is None:
            raise ValueError(f'SKILL.md for {skill_id} has invalid frontmatter')
        enabled_lines = [
            index for index, line in enumerate(lines[1:closing], start=1)
            if not line.startswith((' ', '\t'))
            and line.partition(':')[0].strip() == 'enabled'
        ]
        if len(enabled_lines) > 1:
            raise ValueError(
                f'SKILL.md for {skill_id} has duplicate enabled fields')
        newline = '\r\n' if lines[0].endswith('\r\n') else '\n'
        rendered = f'enabled: {str(next_enabled).lower()}{newline}'
        if enabled_lines:
            lines[enabled_lines[0]] = rendered
        else:
            lines.insert(closing, rendered)
        write_text_atomic(filepath, ''.join(lines))
    _invalidate_skills_cache()
    updated = _memory_from_file(
        filepath, scope=skill.get('scope', 'project'),
        package_dir=skill.get('package_dir') or None,
        memory_id_override=skill_id, owner_user_id=owner_user_id)
    return _refresh_eligibility([updated], owner_user_id)[0] if updated else None
