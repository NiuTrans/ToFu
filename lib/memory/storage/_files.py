"""lib/memory/storage/_files.py — Per-memory-file I/O + eligibility gating.

Reads/writes individual memory markdown files and enumerates a directory.
Depends on :mod:`_frontmatter` (parse/build) and :mod:`_dirs` (``_ensure_dir``).
"""

import os
import re
import shutil
import uuid
from datetime import datetime, timezone

from lib.json_store import write_text_atomic
from lib.log import get_logger

from ._dirs import _ensure_dir
from ._frontmatter import (
    _build_frontmatter,
    _coerce_str_list,
    _extract_package_metadata,
    _parse_frontmatter,
)

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════
#  Memory Eligibility Gating (OpenClaw-inspired)
# ═══════════════════════════════════════════════════════

def _check_memory_eligible(mem):
    """Check whether a memory's runtime requirements are satisfied.

    Honours ``always=True`` (skip all gates) and:
      * ``requires_bins``       — every binary must be on PATH.
      * ``requires_any_bins``   — at least one binary must be on PATH.
      * ``requires_env``        — every env var must be set.
      * ``requires_os``         — current platform must match (``darwin`` /
                                  ``linux`` / ``win32``).

    Returns (eligible: bool, reasons: list[str]).
    """
    if mem.get('always'):
        return True, []

    reasons = []
    required_bins = _coerce_str_list(mem.get('requires_bins'))
    for binary in required_bins:
        if not shutil.which(binary):
            reasons.append(f'binary `{binary}` not found on PATH')

    any_bins = _coerce_str_list(mem.get('requires_any_bins'))
    if any_bins and not any(shutil.which(b) for b in any_bins):
        reasons.append('none of `' + '`/`'.join(any_bins) + '` found on PATH')

    required_env = _coerce_str_list(mem.get('requires_env'))
    for var in required_env:
        if os.environ.get(var):
            continue
        # Skill packages get a second source: the credential vault, where the
        # user configures per-skill keys in Settings → Skills. A configured
        # key satisfies the gate exactly like a process env var.
        if mem.get('is_package') and mem.get('id'):
            try:
                from lib.skills.env import vault_has_env
                if vault_has_env(mem['id'], var):
                    continue
            except Exception as e:
                logger.debug('vault env probe failed for %s: %s', var, e)
        reasons.append(f'env var `{var}` not set')

    required_os = _coerce_str_list(mem.get('requires_os'))
    if required_os:
        import sys
        plat_map = {'linux': 'linux', 'darwin': 'darwin', 'win32': 'win32'}
        cur = plat_map.get(sys.platform, sys.platform)
        if not any(o == cur for o in required_os):
            reasons.append(f'requires OS in {required_os}; current={cur}')

    return (len(reasons) == 0), reasons


# ═══════════════════════════════════════════════════════
#  Memory File I/O
# ═══════════════════════════════════════════════════════

def _memory_from_file(filepath, scope='global', package_dir=None,
                       memory_id_override=None):
    """Read a single memory file and return a memory dict.

    Args:
        filepath: Path to a ``.md`` file (flat memory) or a package
            ``SKILL.md`` (when ``package_dir`` is provided).
        scope: ``'global'`` or ``'project'``.
        package_dir: When the memory is a directory-style skill package,
            the path to the package root (containing ``SKILL.md``,
            ``references/``, ``scripts/`` etc.).  ``None`` for flat memories.
        memory_id_override: Force a specific id (used for package skills
            where the directory name is the id, not the filename).
    """
    try:
        with open(filepath, encoding='utf-8') as f:
            text = f.read()
    except OSError:
        logger.debug('Failed to read memory file %s', filepath, exc_info=True)
        return None

    meta, body = _parse_frontmatter(text)
    if memory_id_override:
        memory_id = memory_id_override
    else:
        memory_id = os.path.splitext(os.path.basename(filepath))[0]

    # Pull OpenClaw / Anthropic-style gating fields out of metadata.
    pkg_meta = _extract_package_metadata(meta)

    # Packages installed from the curated catalog drop a ``.catalog_id``
    # marker so the catalog endpoint can match them back (the memory id is
    # derived from SKILL.md ``name`` and rarely equals the catalog id).
    catalog_id = ''
    if package_dir:
        marker = os.path.join(package_dir, '.catalog_id')
        if os.path.isfile(marker):
            try:
                with open(marker, encoding='utf-8') as cf:
                    catalog_id = cf.read().strip()
            except OSError as e:
                logger.debug('Failed to read .catalog_id in %s: %s',
                             package_dir, e)

    # Top-level frontmatter overrides (``requires_bins:`` /
    # ``requires_env:`` directly in frontmatter, predating the
    # ``metadata.openclaw`` block format).
    legacy_bins = _coerce_str_list(meta.get('requires_bins'))
    legacy_env = _coerce_str_list(meta.get('requires_env'))

    mem = {
        'id': memory_id,
        'name': meta.get('name', memory_id.replace('_', ' ').replace('-', ' ').title()),
        'description': meta.get('description', ''),
        'enabled': meta.get('enabled', True),
        'tags': meta.get('tags', []),
        'requires_bins': legacy_bins or pkg_meta['requires_bins'],
        'requires_any_bins': pkg_meta['requires_any_bins'],
        'requires_env': legacy_env or pkg_meta['requires_env'],
        'requires_os': pkg_meta['requires_os'],
        'always': pkg_meta['always'],
        'homepage': pkg_meta['homepage'],
        'primary_env': pkg_meta['primary_env'],
        'install_specs': pkg_meta['install_specs'],
        'created': meta.get('created', ''),
        'updated': meta.get('updated', ''),
        'scope': scope,
        'body': body.strip(),
        'filepath': filepath,
        'is_package': bool(package_dir),
        'package_dir': package_dir or '',
        'catalog_id': catalog_id,
    }

    eligible, reasons = _check_memory_eligible(mem)
    mem['eligible'] = eligible
    mem['ineligible_reasons'] = reasons
    return mem


def _write_memory_file(filepath, mem):
    """Write a memory dict back to a markdown file."""
    _ensure_dir(os.path.dirname(filepath))
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    meta = {
        'name': mem.get('name', 'Untitled Memory'),
        'description': mem.get('description', ''),
        'enabled': mem.get('enabled', True),
        'tags': mem.get('tags', []),
        'created': mem.get('created', now),
        'updated': now,
    }
    if mem.get('requires_bins'):
        meta['requires_bins'] = mem['requires_bins']
    if mem.get('requires_env'):
        meta['requires_env'] = mem['requires_env']

    body = mem.get('body', '')
    content = _build_frontmatter(meta) + '\n' + body + '\n'

    write_text_atomic(filepath, content)
    return now


# ═══════════════════════════════════════════════════════
#  List / Load Memories
# ═══════════════════════════════════════════════════════

def _list_skill_packages_in_dir(dirpath, scope='global'):
    """Enumerate skill packages (``<dirpath>/<id>/SKILL.md``) in a directory.

    This is the skills-channel view: ONLY package directories are returned,
    flat ``*.md`` memories are ignored. Sub-files (references, scripts,
    knowledge) are NOT indexed individually — they are reachable via
    Progressive Disclosure once the SKILL.md is in scope.

    The ``global`` sub-directory is excluded when scanning a project root —
    it is enumerated separately as scope='global'.
    """
    packages = []
    if not os.path.isdir(dirpath):
        return packages

    for entry in sorted(os.listdir(dirpath)):
        if entry.startswith('.'):
            continue
        full = os.path.join(dirpath, entry)

        # Skip the 'global' sub-directory when listing project scope —
        # global entries are listed via their own enumeration.
        if scope == 'project' and entry == 'global' and os.path.isdir(full):
            continue

        if os.path.isdir(full):
            skill_md = os.path.join(full, 'SKILL.md')
            if os.path.isfile(skill_md):
                mem = _memory_from_file(
                    skill_md, scope=scope,
                    package_dir=full,
                    memory_id_override=entry,
                )
                if mem:
                    packages.append(mem)
    return packages


def _list_memories_in_dir(dirpath, scope='global'):
    """List memories in a directory.

    Discovers two physical layouts:
      * **Flat memory**         — ``<dirpath>/<id>.md``
      * **Skill package**       — ``<dirpath>/<id>/SKILL.md`` (via
        :func:`_list_skill_packages_in_dir`).

    The ``global`` sub-directory is excluded when scanning the project
    root — it is enumerated separately as scope='global'.
    """
    memories = []
    if not os.path.isdir(dirpath):
        return memories

    for entry in sorted(os.listdir(dirpath)):
        if entry.startswith('.'):
            continue
        full = os.path.join(dirpath, entry)

        # Skip the 'global' sub-directory when listing project scope —
        # global memories are listed via their own enumeration.
        if scope == 'project' and entry == 'global' and os.path.isdir(full):
            continue

        if os.path.isfile(full) and entry.endswith('.md'):
            mem = _memory_from_file(full, scope=scope)
            if mem:
                memories.append(mem)
            continue

    memories.extend(_list_skill_packages_in_dir(dirpath, scope=scope))
    return memories


def _make_memory_id(name):
    """Generate a filesystem-safe ID from a memory name."""
    safe = re.sub(r'[^\w\s-]', '', name.lower())
    safe = re.sub(r'[\s]+', '_', safe).strip('_')
    if not safe:
        safe = uuid.uuid4().hex[:8]
    return safe
