"""lib/memory/installer.py \u2014 Drag-and-drop install of skill packages.

A *skill package* is a directory containing at least a ``SKILL.md`` file
with YAML frontmatter (Anthropic Skills / OpenClaw / mlp-skills format).
Sub-files (``references/``, ``scripts/``, ``knowledge/``, ``assets/``) are
copied verbatim and reachable via Progressive Disclosure once the model
chooses to ``read_files`` them.

This module accepts a ``.zip`` or a directory layout that may wrap the
package one extra level (``citadel.zip`` extracts to ``citadel/citadel/``
with the inner directory holding ``SKILL.md``).  The installer auto-walks
the archive tree to find the package root.

Security policy
---------------
* Maximum unpacked size: 25 MB total, 2000 file entries.
* No path traversal: every entry must resolve inside the temporary
  extraction directory.  Symlinks are rejected.
* ``install.sh`` and other shell scripts are copied **but not executed**
  \u2014 the installer surfaces them as ``install_hint`` so the user can
  inspect / run them manually.
* All edits commit through :func:`shutil.copytree` to the project's
  ``.tofu/skills/`` tree.  No filesystem writes outside that tree.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import zipfile
from typing import Any

from lib.log import audit_log, get_logger
from lib.memory.storage import (
    _make_memory_id,
    _memory_from_file,
    _parse_frontmatter,
    resolve_skills_dir,
)

logger = get_logger(__name__)

__all__ = ['install_skill_package', 'InstallerError']


# Hard upper bounds (zip-bomb defence).  Skill packages we've seen in
# the wild range from a few KB (single SKILL.md) up to ~150 KB
# (mlp-skills with 100+ files).  25 MB is conservative head-room.
_MAX_BYTES = 25 * 1024 * 1024
_MAX_FILES = 2000

# Files that should NEVER be installed even if present in the archive.
_DENYLIST_NAMES = frozenset({
    '.DS_Store', 'Thumbs.db', '.git', '.svn',
})


class InstallerError(Exception):
    """Raised when a skill package fails validation."""


# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
#  Helpers
# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550

def _safe_extract_zip(zip_obj: zipfile.ZipFile, dest_dir: str) -> int:
    """Extract a zip into ``dest_dir`` with path-traversal + size guards.

    Returns the number of files extracted.  Raises :class:`InstallerError`
    on any safety violation.
    """
    dest_real = os.path.realpath(dest_dir)
    total_bytes = 0
    file_count = 0

    for info in zip_obj.infolist():
        # Reject zip entries that try to escape the extraction root.
        target = os.path.realpath(os.path.join(dest_dir, info.filename))
        if not (target == dest_real or target.startswith(dest_real + os.sep)):
            raise InstallerError(
                f'Unsafe zip entry (path traversal): {info.filename}'
            )

        # Reject symlinks (mode field encodes them).
        mode = (info.external_attr >> 16) & 0xFFFF
        if mode and (mode & 0o170000) == 0o120000:
            raise InstallerError(f'Symlink in archive rejected: {info.filename}')

        # Skip directory entries \u2014 zipfile.extract creates parents.
        if info.is_dir():
            continue

        # Denylist filenames (tracker droppings, VCS).
        base = os.path.basename(info.filename)
        if base in _DENYLIST_NAMES:
            continue

        total_bytes += info.file_size
        file_count += 1
        if total_bytes > _MAX_BYTES:
            raise InstallerError(
                f'Archive exceeds {_MAX_BYTES // (1024 * 1024)} MB unpacked '
                f'(at {info.filename})'
            )
        if file_count > _MAX_FILES:
            raise InstallerError(f'Archive exceeds {_MAX_FILES} files')

        zip_obj.extract(info, dest_dir)

    return file_count


def _find_skill_root_in_subdir(start_dir: str, subdir: str) -> str | None:
    """Locate a specific sub-skill inside an extracted multi-skill archive.

    Mono-repos like ``anthropics/skills`` ship every sub-skill under
    ``skills/<name>/SKILL.md`` and the zip wraps them one level deep
    (``skills-main/skills/pptx/SKILL.md``).  ``subdir`` is the
    repo-relative path of the wanted skill (e.g. ``'skills/pptx'``); we
    return the first directory whose path ENDS with that suffix and holds
    a ``SKILL.md``.  Returns ``None`` when no such directory exists.
    """
    want = subdir.strip('/').replace('\\', '/')
    want_parts = tuple(want.split('/'))
    for root, _dirs, files in os.walk(start_dir):
        if 'SKILL.md' not in files:
            continue
        rel = os.path.relpath(root, start_dir).replace('\\', '/')
        rel_parts = tuple(p for p in rel.split('/') if p and p != '.')
        if rel_parts[-len(want_parts):] == want_parts:
            return root
    return None


def _find_skill_root(start_dir: str) -> str | None:
    """Locate the directory containing ``SKILL.md`` inside ``start_dir``.

    Handles wrappers like ``citadel.zip`` -> ``citadel/citadel/SKILL.md`` by
    descending into single-child directories.  Returns ``None`` if no
    SKILL.md is found at any level.
    """
    if os.path.isfile(os.path.join(start_dir, 'SKILL.md')):
        return start_dir

    # First, try the most-common case: a single sub-directory wrapping
    # the package.  We descend at most 3 levels.
    cur = start_dir
    for _ in range(3):
        try:
            entries = [e for e in os.listdir(cur) if not e.startswith('.')]
        except OSError as _e_audit:
            logger.debug('[installer] _find_skill_root caught %s: %s', type(_e_audit).__name__, _e_audit)
            break
        # If exactly one sub-directory and no SKILL.md here, descend.
        sub_dirs = [e for e in entries if os.path.isdir(os.path.join(cur, e))]
        has_skill = os.path.isfile(os.path.join(cur, 'SKILL.md'))
        if has_skill:
            return cur
        if len(sub_dirs) == 1 and len(entries) <= 4:
            cur = os.path.join(cur, sub_dirs[0])
            continue
        break

    if os.path.isfile(os.path.join(cur, 'SKILL.md')):
        return cur

    # Fallback: BFS, return first SKILL.md.
    for root, dirs, files in os.walk(start_dir):
        if 'SKILL.md' in files:
            return root
    return None


def _validate_skill_md(path: str) -> dict[str, Any]:
    """Parse SKILL.md and ensure it has the required ``name`` and
    ``description`` frontmatter keys.  Returns the parsed meta dict.
    """
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except OSError as e:
        raise InstallerError(f'Cannot read SKILL.md: {e}') from e

    meta, _ = _parse_frontmatter(text)
    name = (meta.get('name') or '').strip()
    desc = (meta.get('description') or '').strip()
    if not name:
        raise InstallerError('SKILL.md frontmatter missing required key: name')
    if not desc:
        raise InstallerError(
            'SKILL.md frontmatter missing required key: description'
        )
    return meta


def _detect_install_hints(skill_root: str) -> list[dict[str, str]]:
    """Return a list of hint entries for any installer scripts present.

    We deliberately do NOT execute these \u2014 they often need flags
    (e.g. ``install-openclaw.sh <friday_appid>``) and may install global
    packages or modify ``~/.bashrc``.
    """
    hints = []
    for fname in ('install.sh', 'install-cc.sh', 'install-openclaw.sh'):
        full = os.path.join(skill_root, fname)
        if os.path.isfile(full):
            hints.append({
                'file': fname,
                'note': (
                    'Installer script present but NOT auto-executed for '
                    'safety. Inspect and run manually if needed.'
                ),
            })
    return hints


# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
#  Public API
# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550

def install_skill_package(
    source: str | bytes,
    *,
    scope: str = 'project',
    project_path: str | None = None,
    overwrite: bool = False,
    original_filename: str | None = None,
    catalog_id: str | None = None,
    subdir: str | None = None,
) -> dict[str, Any]:
    """Install a skill package into the memory tree.

    Args:
        source: Either an absolute filesystem path (zip or directory)
            or raw bytes of an uploaded zip.
        scope: ``'project'`` or ``'global'``.
        project_path: The project root (required for both scopes; see
            :func:`lib.memory.storage.resolve_skills_dir`).
        overwrite: When ``True``, replace an existing package with the
            same id.  When ``False`` (default), the new package is
            installed under ``<id>_2``, ``<id>_3``, ... to avoid clobber.
        original_filename: Optional — used purely for logging when
            ``source`` is bytes.
        catalog_id: Optional — when installing from the curated catalog,
            the catalog entry id (e.g. ``'xlsx-skill'``). Persisted in a
            ``.catalog_id`` marker inside the package so the catalog can
            later detect that this entry is installed (the SKILL.md-derived
            memory id rarely equals the catalog id).
        subdir: Optional — repo-relative path of a single sub-skill to
            install from a multi-skill archive (e.g. ``'skills/pptx'`` in
            the ``anthropics/skills`` mono-repo). When given, only that
            sub-directory is installed instead of the first ``SKILL.md``
            the breadth-first walk happens to reach.

    Returns:
        ``{'memory': <memory dict>, 'install_hints': [...], 'replaced': bool}``

    Raises:
        InstallerError on any validation failure.  Caller should map this
        to an HTTP 400 response.
    """
    if scope not in ('project', 'global'):
        raise InstallerError(f'Invalid scope: {scope!r}')

    log_label = original_filename or (
        os.path.basename(source) if isinstance(source, str) else '<uploaded>')
    logger.info('[SkillInstaller] Installing %s (scope=%s)', log_label, scope)

    with tempfile.TemporaryDirectory(prefix='tofu-skill-') as tmp:
        # \u2500\u2500 Step 1: materialise the package into ``tmp/extracted/`` \u2500\u2500
        extracted = os.path.join(tmp, 'extracted')
        os.makedirs(extracted, exist_ok=True)

        if isinstance(source, (bytes, bytearray)):
            with zipfile.ZipFile(io.BytesIO(source)) as zf:
                n = _safe_extract_zip(zf, extracted)
            logger.debug('[SkillInstaller] Extracted %d files from bytes', n)
        elif isinstance(source, str) and os.path.isdir(source):
            # Directory source \u2014 copy in (excluding heavy junk).
            shutil.copytree(source, extracted, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*_DENYLIST_NAMES))
        elif isinstance(source, str) and os.path.isfile(source):
            if not zipfile.is_zipfile(source):
                raise InstallerError(
                    f'Not a zip archive: {source} (only .zip and directory '
                    'sources are supported)'
                )
            with zipfile.ZipFile(source) as zf:
                n = _safe_extract_zip(zf, extracted)
            logger.debug('[SkillInstaller] Extracted %d files from %s',
                         n, source)
        else:
            raise InstallerError(f'Cannot read source: {source!r}')

        # \u2500\u2500 Step 2: locate SKILL.md \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        if subdir:
            skill_root = _find_skill_root_in_subdir(extracted, subdir)
            if not skill_root:
                raise InstallerError(
                    f'Sub-skill {subdir!r} not found in the archive. The '
                    'catalog entry may reference a path that no longer '
                    'exists upstream.'
                )
        else:
            skill_root = _find_skill_root(extracted)
        if not skill_root:
            raise InstallerError(
                'No SKILL.md found in the package. A skill package must '
                'contain a SKILL.md at its root (Anthropic / OpenClaw '
                'AgentSkills format).'
            )

        skill_md = os.path.join(skill_root, 'SKILL.md')
        meta = _validate_skill_md(skill_md)
        skill_name = (meta.get('name') or '').strip()
        skill_id = _make_memory_id(skill_name)

        # \u2500\u2500 Step 3: pick destination directory \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        target_root = resolve_skills_dir(scope, project_path)
        os.makedirs(target_root, exist_ok=True)

        replaced = False
        target_dir = os.path.join(target_root, skill_id)
        if os.path.exists(target_dir):
            if overwrite:
                logger.warning(
                    '[SkillInstaller] Overwriting existing package: %s',
                    target_dir,
                )
                shutil.rmtree(target_dir)
                replaced = True
            else:
                # Find a free suffix: <id>_2, <id>_3, ...
                counter = 2
                while True:
                    candidate = os.path.join(target_root, f'{skill_id}_{counter}')
                    if not os.path.exists(candidate):
                        target_dir = candidate
                        skill_id = f'{skill_id}_{counter}'
                        break
                    counter += 1

        # Defensive: confirm target stays inside target_root.
        if not os.path.realpath(target_dir).startswith(
                os.path.realpath(target_root)):
            raise InstallerError('Resolved install path escapes skills root')

        # \u2500\u2500 Step 4: copy package into place \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        shutil.copytree(skill_root, target_dir,
                        ignore=shutil.ignore_patterns(*_DENYLIST_NAMES))
        logger.info(
            '[SkillInstaller] Installed package %s into %s', skill_id,
            target_dir,
        )

        # Record the catalog origin so the catalog endpoint can mark this
        # entry as installed — the SKILL.md-derived ``skill_id`` rarely
        # matches the catalog id (e.g. catalog ``xlsx-skill`` → id ``xlsx``).
        # Written BEFORE _memory_from_file so the returned dict picks it up.
        if catalog_id:
            try:
                with open(os.path.join(target_dir, '.catalog_id'), 'w',
                          encoding='utf-8') as cf:
                    cf.write(catalog_id.strip() + '\n')
            except OSError as e:
                logger.warning('[SkillInstaller] Failed to write .catalog_id '
                               'marker for %s: %s', skill_id, e)

        # \u2500\u2500 Step 5: build the memory dict for the response \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        mem = _memory_from_file(
            os.path.join(target_dir, 'SKILL.md'),
            scope=scope,
            package_dir=target_dir,
            memory_id_override=skill_id,
        )
        if not mem:
            # Should not happen \u2014 we just copied a valid SKILL.md.
            raise InstallerError('Failed to load SKILL.md after install')

        install_hints = _detect_install_hints(target_dir)

        audit_log(
            'skill_install',
            skill_id=skill_id,
            scope=scope,
            package_dir=target_dir,
            replaced=replaced,
            install_hints=[h['file'] for h in install_hints],
        )

        return {
            'memory': mem,
            'install_hints': install_hints,
            'replaced': replaced,
            'catalog_id': catalog_id or '',
        }
