"""lib/memory/storage/_files.py — Per-memory-file I/O + eligibility gating.

Reads/writes individual memory markdown files and enumerates a directory.
Depends on :mod:`_frontmatter` (parse/build) and :mod:`_dirs` (``_ensure_dir``).
"""

import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone

from lib.json_store import write_text_atomic
from lib.log import get_logger
from lib.memory.contracts import (
    MEMORY_FRONTMATTER_READ_MAX_CHARS,
    MEMORY_GENERATED_ID_MAX_BYTES,
    truncate_utf8,
)
from lib.memory.resource_policy import memory_metadata_cache_budget

from ._dirs import _ensure_dir
from ._frontmatter import (
    _build_frontmatter,
    _copy_metadata_value,
    _coerce_str_list,
    _extract_package_metadata,
    _parse_frontmatter,
)
from ._metadata_cache import MemoryFileFingerprint, MemoryMetadataCache

logger = get_logger(__name__)

_metadata_cache_entries, _metadata_cache_bytes = memory_metadata_cache_budget()
_metadata_cache = MemoryMetadataCache(
    max_entries=_metadata_cache_entries,
    max_bytes=_metadata_cache_bytes,
)

_MEMORY_RECORD_VIEWS = frozenset({'complete', 'retrieval'})
_MEMORY_RETRIEVAL_RECORD_KEYS = (
    'id',
    'name',
    'description',
    'enabled',
    'tags',
    'scope',
    'filepath',
    'is_package',
    'package_dir',
    'eligible',
)


class _MemoryEnvelopeLimitError(ValueError):
    """A frontmatter envelope exceeded its bounded metadata read budget."""


# ═══════════════════════════════════════════════════════
#  Memory Eligibility Gating (OpenClaw-inspired)
# ═══════════════════════════════════════════════════════

def _check_memory_eligible(mem, owner_user_id=None):
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
                if vault_has_env(
                        mem['id'], var, owner_user_id=owner_user_id):
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

def _read_memory_source(
    filepath,
    *,
    include_body=True,
    body_char_limit=None,
):
    """Read a memory document or only its closed frontmatter envelope.

    Metadata-only list callers do not need the Markdown body.  Stop after the
    closing ``---`` instead of materializing the remainder; a document without
    valid opening/closing delimiters has the same empty metadata it would have
    after a full parse. ``body_char_limit`` reads one bounded body prefix after
    a bounded frontmatter envelope; omitted limits preserve full detail and
    mutation behavior for existing durable files.
    """
    if body_char_limit is not None:
        if (isinstance(body_char_limit, bool)
                or not isinstance(body_char_limit, int)
                or body_char_limit < 0):
            raise ValueError('body_char_limit must be a non-negative integer')
    with open(filepath, encoding='utf-8') as source:
        if include_body and body_char_limit is None:
            return source.read()

        first_line_budget = MEMORY_FRONTMATTER_READ_MAX_CHARS
        if include_body:
            first_line_budget = min(
                first_line_budget, max(4, body_char_limit))
        first_line = source.readline(first_line_budget + 1)
        if len(first_line) > first_line_budget:
            if include_body:
                return first_line[:body_char_limit]
            if first_line.startswith('---'):
                raise _MemoryEnvelopeLimitError(
                    'memory frontmatter exceeds the '
                    f'{MEMORY_FRONTMATTER_READ_MAX_CHARS:,}-character limit')
            return ''
        if first_line.strip() != '---':
            if not include_body:
                return ''
            remaining = max(0, body_char_limit - len(first_line))
            return (first_line + source.read(remaining))[:body_char_limit]

        frontmatter_lines = [first_line]
        retained_chars = len(first_line)
        while retained_chars <= MEMORY_FRONTMATTER_READ_MAX_CHARS:
            remaining = MEMORY_FRONTMATTER_READ_MAX_CHARS - retained_chars
            line = source.readline(remaining + 1)
            if not line:
                return ''
            if len(line) > remaining:
                raise _MemoryEnvelopeLimitError(
                    'memory frontmatter exceeds the '
                    f'{MEMORY_FRONTMATTER_READ_MAX_CHARS:,}-character limit')
            frontmatter_lines.append(line)
            retained_chars += len(line)
            if line.strip() == '---':
                envelope = ''.join(frontmatter_lines)
                if not include_body:
                    return envelope
                return envelope + source.read(body_char_limit)
        return ''


def _memory_file_fingerprint_from_stat(stat) -> MemoryFileFingerprint:
    """Normalize one stat result into the metadata-cache revision tuple."""
    return (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
    )


def _memory_file_fingerprint(filepath: str) -> MemoryFileFingerprint:
    return _memory_file_fingerprint_from_stat(os.stat(filepath))


def _validate_memory_record_view(record_view, *, include_body):
    if record_view not in _MEMORY_RECORD_VIEWS:
        raise ValueError(
            "record_view must be 'complete' or 'retrieval'")
    if record_view == 'retrieval' and include_body:
        raise ValueError(
            "record_view='retrieval' requires include_body=False")


def _project_memory_record(memory, record_view):
    if record_view == 'complete':
        return memory
    return {
        key: memory[key]
        for key in _MEMORY_RETRIEVAL_RECORD_KEYS
    }


def _memory_from_file(filepath, scope='global', package_dir=None,
                       memory_id_override=None, owner_user_id=None,
                       include_body=True, body_char_limit=None,
                       fingerprint_hint=None, record_view='complete'):
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
        owner_user_id: Optional owner for package credential eligibility.
        include_body: Read and return the Markdown body.  Summary-list callers
            set this false so file I/O stops at closed frontmatter.
        body_char_limit: Optional body-prefix bound for derived ranking or
            context views. Full durable reads remain the default.
        fingerprint_hint: Optional stat fingerprint captured from the same
            directory snapshot. Metadata lists use it to avoid repeating the
            pre-read stat; a cold read still verifies the post-read revision.
        record_view: ``complete`` preserves the durable/API record. The
            metadata-only ``retrieval`` view retains only ranking, eligibility,
            provenance, and exact-hydration fields.
    """
    _validate_memory_record_view(record_view, include_body=include_body)
    try:
        if include_body:
            if body_char_limit is None:
                text = _read_memory_source(filepath, include_body=True)
            else:
                text = _read_memory_source(
                    filepath,
                    include_body=True,
                    body_char_limit=body_char_limit,
                )
            meta, body = _parse_frontmatter(text)
        else:
            identity = os.path.abspath(filepath)
            fingerprint_before = (
                fingerprint_hint
                if fingerprint_hint is not None
                else _memory_file_fingerprint(filepath)
            )
            cached, meta = _metadata_cache.lookup_readonly(
                identity, fingerprint_before)
            body = ''
            if not cached:
                text = _read_memory_source(filepath, include_body=False)
                meta, _ignored_body = _parse_frontmatter(text)
                fingerprint_after = _memory_file_fingerprint(filepath)
                if fingerprint_after == fingerprint_before:
                    _metadata_cache.store(
                        identity, fingerprint_after, meta)
    except (OSError, UnicodeError, _MemoryEnvelopeLimitError):
        logger.debug('Failed to read memory file %s', filepath, exc_info=True)
        return None
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
    origin = {}
    if package_dir:
        marker = os.path.join(package_dir, '.catalog_id')
        if os.path.isfile(marker):
            try:
                with open(marker, encoding='utf-8') as cf:
                    raw_catalog_id = cf.read(129).strip()
                if len(raw_catalog_id) <= 128:
                    catalog_id = raw_catalog_id
            except OSError as e:
                logger.debug('Failed to read .catalog_id in %s: %s',
                             package_dir, e)
        origin_path = os.path.join(package_dir, '.skill-origin.json')
        if os.path.isfile(origin_path) and not os.path.islink(origin_path):
            try:
                with open(origin_path, encoding='utf-8') as origin_file:
                    raw_origin = origin_file.read(4_097)
                if len(raw_origin) <= 4_096:
                    parsed_origin = json.loads(raw_origin)
                    if isinstance(parsed_origin, dict):
                        origin = parsed_origin
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
                logger.debug('Failed to read skill origin in %s: %s',
                             package_dir, e)

    # Top-level frontmatter overrides (``requires_bins:`` /
    # ``requires_env:`` directly in frontmatter, predating the
    # ``metadata.openclaw`` block format).
    legacy_bins = _coerce_str_list(meta.get('requires_bins'))
    legacy_env = _coerce_str_list(meta.get('requires_env'))

    mem = {
        'id': memory_id,
        'name': str(_copy_metadata_value(meta.get('name')) or
                    memory_id.replace('_', ' ').replace('-', ' ').title()),
        'description': str(_copy_metadata_value(
            meta.get('description')) or ''),
        'enabled': _copy_metadata_value(meta.get('enabled', True)),
        'tags': _coerce_str_list(meta.get('tags')),
        'requires_bins': legacy_bins or pkg_meta['requires_bins'],
        'requires_any_bins': pkg_meta['requires_any_bins'],
        'requires_env': legacy_env or pkg_meta['requires_env'],
        'requires_os': pkg_meta['requires_os'],
        'always': pkg_meta['always'],
        'homepage': pkg_meta['homepage'],
        'primary_env': pkg_meta['primary_env'],
        'install_specs': pkg_meta['install_specs'],
        'created': _copy_metadata_value(meta.get('created', '')),
        'updated': _copy_metadata_value(meta.get('updated', '')),
        'scope': scope,
        'body': body.strip(),
        'filepath': filepath,
        'is_package': bool(package_dir),
        'package_dir': package_dir or '',
        'catalog_id': catalog_id,
        'source_revision': str(origin.get('source_revision') or '')[:128],
        'source_registry': str(origin.get('source_registry') or '')[:64],
        'source_url': str(origin.get('source_url') or '')[:2_048],
        'content_sha256': str(origin.get('content_sha256') or '')[:64],
    }

    eligible, reasons = _check_memory_eligible(
        mem, owner_user_id=owner_user_id)
    mem['eligible'] = eligible
    mem['ineligible_reasons'] = reasons
    return _project_memory_record(mem, record_view)


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

def _sorted_visible_dir_entries(dirpath):
    """Return one closed, name-sorted DirEntry snapshot; fail soft on I/O."""
    try:
        with os.scandir(dirpath) as iterator:
            return sorted(
                (entry for entry in iterator
                 if not entry.name.startswith('.')),
                key=lambda entry: entry.name,
            )
    except OSError:
        logger.debug('Failed to enumerate memory directory %s', dirpath,
                     exc_info=True)
        return []


def _memory_package_from_entry(
    entry,
    scope,
    include_body,
    record_view='complete',
):
    """Load one package DirEntry while preserving package/listing gates."""
    try:
        if not entry.is_dir(follow_symlinks=False):
            return None
    except OSError:
        return None
    if scope == 'project' and entry.name == 'global':
        return None
    package_dir = entry.path
    skill_md = os.path.join(package_dir, 'SKILL.md')
    if not (os.path.isfile(skill_md) and not os.path.islink(skill_md)):
        return None
    return _memory_from_file(
        skill_md,
        scope=scope,
        package_dir=package_dir,
        memory_id_override=entry.name,
        include_body=include_body,
        record_view=record_view,
    )

def _list_skill_packages_in_dir(
    dirpath,
    scope='global',
    include_body=True,
    record_view='complete',
):
    """Enumerate skill packages (``<dirpath>/<id>/SKILL.md``) in a directory.

    This is the skills-channel view: ONLY package directories are returned,
    flat ``*.md`` memories are ignored. Sub-files (references, scripts,
    knowledge) are NOT indexed individually — they are reachable via
    Progressive Disclosure once the SKILL.md is in scope.

    The ``global`` sub-directory is excluded when scanning a project root —
    it is enumerated separately as scope='global'.
    """
    packages = []
    for entry in _sorted_visible_dir_entries(dirpath):
        memory = _memory_package_from_entry(
            entry, scope, include_body, record_view)
        if memory is not None:
            packages.append(memory)
    return packages


def _list_memories_in_dir(
    dirpath,
    scope='global',
    include_body=True,
    record_view='complete',
):
    """List memories in a directory.

    Discovers two physical layouts:
      * **Flat memory**         — ``<dirpath>/<id>.md``
      * **Skill package**       — ``<dirpath>/<id>/SKILL.md`` (via
        :func:`_list_skill_packages_in_dir`).

    The ``global`` sub-directory is excluded when scanning the project
    root — it is enumerated separately as scope='global'.
    """
    flat_memories = []
    package_entries = []
    for entry in _sorted_visible_dir_entries(dirpath):
        try:
            if (entry.name.endswith('.md')
                    and entry.is_file(follow_symlinks=False)):
                fingerprint_hint = None
                if not include_body:
                    fingerprint_hint = _memory_file_fingerprint_from_stat(
                        entry.stat(follow_symlinks=False))
                memory = _memory_from_file(
                    entry.path,
                    scope=scope,
                    include_body=include_body,
                    fingerprint_hint=fingerprint_hint,
                    record_view=record_view,
                )
                if memory is not None:
                    flat_memories.append(memory)
                continue
            package_entries.append(entry)
        except OSError:
            logger.debug('Memory entry disappeared during scan: %s',
                         entry.path, exc_info=True)

    packages = []
    for entry in package_entries:
        memory = _memory_package_from_entry(
            entry, scope, include_body, record_view)
        if memory is not None:
            packages.append(memory)
    return flat_memories + packages


def _memory_summary_from_id_in_dir(dirpath, memory_id, scope='global'):
    """Read one exact flat/package summary without enumerating ``dirpath``.

    Flat memories precede same-ID packages exactly as
    :func:`_list_memories_in_dir` does. Hidden entries and symlinks retain the
    listing contract, and a failed/unreadable flat record does not shadow a
    readable package with the same ID.
    """
    if memory_id.startswith('.'):
        return None

    flat_path = os.path.join(dirpath, f'{memory_id}.md')
    if os.path.isfile(flat_path) and not os.path.islink(flat_path):
        memory = _memory_from_file(
            flat_path, scope=scope, include_body=False)
        if memory is not None:
            return memory

    # Project ``global/`` is a legacy store root, not a package ID; the package
    # enumerator applies this same exclusion while still allowing global.md.
    if scope == 'project' and memory_id == 'global':
        return None
    package_dir = os.path.join(dirpath, memory_id)
    if os.path.isdir(package_dir) and not os.path.islink(package_dir):
        skill_path = os.path.join(package_dir, 'SKILL.md')
        if os.path.isfile(skill_path) and not os.path.islink(skill_path):
            return _memory_from_file(
                skill_path,
                scope=scope,
                package_dir=package_dir,
                memory_id_override=memory_id,
                include_body=False,
            )
    return None


def _make_memory_id(name):
    """Generate a filesystem-safe ID with room for suffixes and ``.md``."""
    safe = re.sub(r'[^\w\s-]', '', name.lower())
    safe = re.sub(r'[\s]+', '_', safe).strip('_')
    safe = truncate_utf8(safe, MEMORY_GENERATED_ID_MAX_BYTES).rstrip('_-')
    if not safe:
        safe = uuid.uuid4().hex[:8]
    return safe
