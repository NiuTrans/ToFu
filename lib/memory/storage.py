"""lib/memory/storage.py — File I/O, YAML frontmatter, CRUD operations.

Memories are plain Markdown files stored in:
  • Global:  <project>/.tofu/skills/global/*.md  (apply across projects)
  • Project: <project>/.tofu/skills/*.md         (project-specific)

All memories live under the project directory.
"""

import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'GLOBAL_MEMORY_DIR', 'GLOBAL_MEMORY_SUBDIR', 'PROJECT_MEMORY_SUBDIR', 'MIN_DESCRIPTION_LENGTH',
    'list_all_memories', 'list_memories', 'get_memory', 'get_enabled_memories',
    'get_eligible_memories',
    'create_memory', 'update_memory', 'delete_memory', 'merge_memories',
    'toggle_memory',
    'resolve_target_dir',
]

# ═══════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════

GLOBAL_MEMORY_SUBDIR = os.path.join('.tofu', 'skills', 'global')
PROJECT_MEMORY_SUBDIR = os.path.join('.tofu', 'skills')
MIN_DESCRIPTION_LENGTH = 20

# Keep GLOBAL_MEMORY_DIR as a computed property for backward compat
# (injection.py references it for the path template)
GLOBAL_MEMORY_DIR = None  # Set dynamically; see _get_global_memory_dir()

_lock = threading.Lock()


# ═══════════════════════════════════════════════════════
#  Frontmatter Parsing
# ═══════════════════════════════════════════════════════

_FM_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)


def _parse_frontmatter(text):
    """Parse YAML-like frontmatter from markdown text. Returns (meta_dict, body).

    Supports:
      - Single-line scalars: ``name: foo``
      - Booleans: ``enabled: true`` / ``yes`` / ``no``
      - Inline lists: ``tags: [a, b]``
      - Quoted strings: ``description: "..."``
      - YAML folded scalars (``description: >`` followed by indented continuation lines)
      - Single-line JSON object after a key: ``metadata: {"openclaw":{...}}``
        (used by Anthropic Skills / OpenClaw / mlp-skills packages)
      - Single-line JSON object spread across multiple indented lines under
        ``metadata:`` — collapsed and parsed as JSON.
    """
    m = _FM_RE.match(text)
    if not m:
        return {}, text

    fm_text = m.group(1)
    body = text[m.end():]
    meta = {}

    raw_lines = fm_text.split('\n')
    i = 0
    while i < len(raw_lines):
        raw = raw_lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith('#'):
            i += 1
            continue
        if ':' not in raw:
            i += 1
            continue

        # Detect indentation of this top-level key — top-level keys have
        # zero leading whitespace; nested lines (e.g. metadata block body)
        # have leading whitespace.
        leading = len(raw) - len(raw.lstrip(' '))
        if leading > 0:
            i += 1
            continue

        key, _, val = raw.partition(':')
        key = key.strip()
        val = val.strip()

        # ── Case A: folded scalar (``key: >``) ─────────────────────────
        if val == '>' or val == '|':
            buf = []
            j = i + 1
            while j < len(raw_lines):
                nxt = raw_lines[j]
                if not nxt.strip():
                    j += 1
                    continue
                if not nxt.startswith((' ', '\t')):
                    break
                buf.append(nxt.strip())
                j += 1
            joined = ' '.join(buf) if val == '>' else '\n'.join(buf)
            meta[key] = joined
            i = j
            continue

        # ── Case B: JSON object (single- or multi-line) ────────────────
        if val.startswith('{'):
            buf = [val]
            depth = val.count('{') - val.count('}')
            j = i + 1
            while depth > 0 and j < len(raw_lines):
                nxt = raw_lines[j]
                buf.append(nxt.strip())
                depth += nxt.count('{') - nxt.count('}')
                j += 1
            joined = ' '.join(buf)
            try:
                meta[key] = json.loads(joined)
            except (json.JSONDecodeError, ValueError) as e:
                logger.debug('Frontmatter JSON parse failed for key=%s: %s',
                             key, e)
                meta[key] = joined  # fall back to raw string
            i = j
            continue

        # ── Case C: scalar / list / boolean ───────────────────────────
        if val.lower() in ('true', 'yes'):
            meta[key] = True
        elif val.lower() in ('false', 'no'):
            meta[key] = False
        elif val.startswith('[') and val.endswith(']'):
            meta[key] = [v.strip().strip('"\'') for v in val[1:-1].split(',') if v.strip()]
        elif (val.startswith('"') and val.endswith('"')) or \
             (val.startswith("'") and val.endswith("'")):
            meta[key] = val[1:-1]
        else:
            meta[key] = val
        i += 1

    return meta, body


def _coerce_str_list(val):
    """Best-effort coerce ``val`` (str | list | None) to a list[str]."""
    if val is None or val == '':
        return []
    if isinstance(val, list):
        return [str(x) for x in val if x]
    return [str(val)]


def _extract_package_metadata(meta):
    """Extract `requires_bins` / `requires_env` / `homepage` / `always` /
    `os` from an Anthropic / OpenClaw-style ``metadata`` block.

    Recognises both ``metadata.openclaw`` and the legacy
    ``metadata.clawdbot`` layout.  Returns a dict with keys::

        requires_bins, requires_env, requires_any_bins,
        requires_os, homepage, always, primary_env, install_specs

    All keys are always present — values default to empty lists / None.
    """
    out = {
        'requires_bins': [],
        'requires_env': [],
        'requires_any_bins': [],
        'requires_os': [],
        'homepage': '',
        'always': False,
        'primary_env': '',
        'install_specs': [],
    }
    md = meta.get('metadata') if isinstance(meta, dict) else None
    if not isinstance(md, dict):
        return out

    block = md.get('openclaw') or md.get('clawdbot') or {}
    if not isinstance(block, dict):
        return out

    requires = block.get('requires') or {}
    if isinstance(requires, dict):
        out['requires_bins'] = _coerce_str_list(requires.get('bins'))
        out['requires_any_bins'] = _coerce_str_list(requires.get('anyBins'))
        out['requires_env'] = _coerce_str_list(requires.get('env'))

    out['requires_os'] = _coerce_str_list(block.get('os'))
    out['homepage'] = str(block.get('homepage') or meta.get('homepage') or '')
    out['always'] = bool(block.get('always'))
    out['primary_env'] = str(block.get('primaryEnv') or '')
    install = block.get('install')
    if isinstance(install, list):
        out['install_specs'] = install
    return out


def _build_frontmatter(meta):
    """Build YAML-like frontmatter string from dict."""
    lines = ['---']
    for key, val in meta.items():
        if isinstance(val, bool):
            lines.append(f'{key}: {"true" if val else "false"}')
        elif isinstance(val, list):
            inner = ', '.join(str(v) for v in val)
            lines.append(f'{key}: [{inner}]')
        else:
            lines.append(f'{key}: {val}')
    lines.append('---')
    return '\n'.join(lines) + '\n'


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
        if not os.environ.get(var):
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

def _ensure_dir(dirpath):
    """Create directory if it doesn't exist."""
    os.makedirs(dirpath, exist_ok=True)


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

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    return now


# ═══════════════════════════════════════════════════════
#  List / Load Memories
# ═══════════════════════════════════════════════════════

def _list_memories_in_dir(dirpath, scope='global'):
    """List memories in a directory.

    Discovers two physical layouts:
      * **Flat memory**         — ``<dirpath>/<id>.md``
      * **Skill package**       — ``<dirpath>/<id>/SKILL.md`` (Anthropic /
        OpenClaw / mlp-skills layout).  Sub-files (references, scripts,
        knowledge) are NOT indexed individually — they are reachable via
        Progressive Disclosure once the SKILL.md is in scope.

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

        if os.path.isdir(full):
            skill_md = os.path.join(full, 'SKILL.md')
            if os.path.isfile(skill_md):
                mem = _memory_from_file(
                    skill_md, scope=scope,
                    package_dir=full,
                    memory_id_override=entry,
                )
                if mem:
                    memories.append(mem)
    return memories


def _get_global_memory_dir(project_path):
    """Return the global memory directory for ``project_path``.

    Global memories live at ``<project>/.tofu/skills/global/``.  Returns
    ``None`` when no project root is set — the caller must handle that
    (typically by skipping global enumeration).
    """
    if not project_path:
        return None
    return os.path.join(project_path, GLOBAL_MEMORY_SUBDIR)


def list_all_memories(project_path=None):
    """List all global + project memories."""
    with _lock:
        global_dir = _get_global_memory_dir(project_path)
        memories = (_list_memories_in_dir(global_dir, scope='global')
                    if global_dir else [])
        if project_path:
            proj_dir = os.path.join(project_path, PROJECT_MEMORY_SUBDIR)
            memories += _list_memories_in_dir(proj_dir, scope='project')
    return memories


def list_memories(project_path=None, scope='all'):
    """List memories, optionally filtered by scope."""
    all_memories = list_all_memories(project_path)
    if scope == 'global':
        return [s for s in all_memories if s['scope'] == 'global']
    elif scope == 'project':
        return [s for s in all_memories if s['scope'] == 'project']
    return all_memories


def get_memory(memory_id, project_path=None):
    """Get a single memory by ID. Returns memory dict or None."""
    for s in list_all_memories(project_path):
        if s['id'] == memory_id:
            return s
    return None


def get_enabled_memories(project_path=None):
    """Get only enabled memories."""
    return [s for s in list_all_memories(project_path) if s.get('enabled', True)]


def get_eligible_memories(project_path=None):
    """Get memories that are both enabled AND meet all runtime requirements."""
    return [
        s for s in get_enabled_memories(project_path)
        if s.get('eligible', True)
    ]


# ═══════════════════════════════════════════════════════
#  CRUD Operations
# ═══════════════════════════════════════════════════════

def _make_memory_id(name):
    """Generate a filesystem-safe ID from a memory name."""
    safe = re.sub(r'[^\w\s-]', '', name.lower())
    safe = re.sub(r'[\s]+', '_', safe).strip('_')
    if not safe:
        safe = uuid.uuid4().hex[:8]
    return safe


def resolve_target_dir(scope, project_path):
    """Return the on-disk directory where a memory of ``scope`` should live.

    Used by both :func:`create_memory` and the package installer.
    Raises ``ValueError`` when ``project_path`` is missing — the in-tree
    layout requires a project root.
    """
    if not project_path:
        raise ValueError('project_path required for memory storage')
    if scope == 'project':
        return os.path.join(project_path, PROJECT_MEMORY_SUBDIR)
    return os.path.join(project_path, GLOBAL_MEMORY_SUBDIR)


def create_memory(name, description='', body='', tags=None, scope='global', project_path=None):
    """Create a new memory file. Returns the memory dict."""
    if description and len(description.strip()) < MIN_DESCRIPTION_LENGTH:
        logger.warning(
            'Memory "%s" has a very short description (%d chars). '
            'Consider making it ≥%d chars for discoverability.',
            name, len(description.strip()), MIN_DESCRIPTION_LENGTH,
        )
    if not description or not description.strip():
        for line in (body or '').split('\n'):
            line = line.strip().lstrip('#').strip()
            if line and len(line) >= 10:
                description = line[:120]
                logger.info('Memory "%s" had no description; auto-set to: %s', name, description)
                break

    memory_id = _make_memory_id(name)
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    mem = {
        'id': memory_id, 'name': name, 'description': description,
        'enabled': True, 'tags': tags or [],
        'requires_bins': [], 'requires_env': [],
        'created': now, 'updated': now, 'body': body, 'scope': scope,
    }

    dirpath = resolve_target_dir(scope, project_path)

    filepath = os.path.join(dirpath, f'{memory_id}.md')
    counter = 1
    # Avoid collisions with both flat .md files AND package directories.
    while os.path.exists(filepath) or os.path.isdir(
            os.path.join(dirpath, mem['id'])):
        filepath = os.path.join(dirpath, f'{memory_id}_{counter}.md')
        mem['id'] = f'{memory_id}_{counter}'
        counter += 1

    _write_memory_file(filepath, mem)
    mem['filepath'] = filepath
    mem['is_package'] = False
    mem['package_dir'] = ''
    return mem


def update_memory(memory_id, updates, project_path=None):
    """Update an existing memory. Returns updated memory or None."""
    all_memories = list_all_memories(project_path)
    target = None
    for s in all_memories:
        if s['id'] == memory_id:
            target = s
            break
    if not target:
        return None
    for key in ('name', 'description', 'body', 'tags', 'enabled',
                'requires_bins', 'requires_env'):
        if key in updates:
            target[key] = updates[key]
    target['updated'] = _write_memory_file(target['filepath'], target)
    return target


def delete_memory(memory_id, project_path=None):
    """Delete a memory. Handles both flat ``.md`` files and package
    directories (``<id>/SKILL.md`` + references/scripts).

    Returns True if deleted.
    """
    all_memories = list_all_memories(project_path)
    for s in all_memories:
        if s['id'] != memory_id:
            continue
        try:
            if s.get('is_package') and s.get('package_dir'):
                pkg = s['package_dir']
                # Defence: only delete inside the project's skills tree.
                if project_path and not os.path.realpath(pkg).startswith(
                        os.path.realpath(project_path)):
                    logger.warning('Refusing to delete package outside project: %s', pkg)
                    return False
                shutil.rmtree(pkg)
                logger.info('[Memory] Removed skill package %s (%s)', memory_id, pkg)
            else:
                os.remove(s['filepath'])
            return True
        except OSError:
            logger.warning('Failed to delete memory %s', s['filepath'], exc_info=True)
            return False
    return False


def merge_memories(memory_ids, name, description, body, tags=None, scope='project', project_path=None):
    """Merge multiple memories into one new consolidated memory, deleting the originals."""
    if not memory_ids or len(memory_ids) < 2:
        raise ValueError("merge_memories requires at least 2 memory IDs")

    all_memories = list_all_memories(project_path)
    mem_map = {s['id']: s for s in all_memories}
    missing = [sid for sid in memory_ids if sid not in mem_map]
    if missing:
        raise ValueError(f"Memories not found: {', '.join(missing)}")

    if tags is None:
        merged_tags = set()
        for sid in memory_ids:
            merged_tags.update(mem_map[sid].get('tags', []))
        tags = sorted(merged_tags)

    merged = create_memory(name=name, description=description, body=body,
                          tags=tags, scope=scope, project_path=project_path)

    deleted_ids = []
    for sid in memory_ids:
        if delete_memory(sid, project_path):
            deleted_ids.append(sid)

    return {'merged_memory': merged, 'deleted_ids': deleted_ids}


def toggle_memory(memory_id, enabled=None, project_path=None):
    """Toggle a memory's enabled state."""
    if enabled is None:
        mem = get_memory(memory_id, project_path)
        if not mem:
            return None
        enabled = not mem.get('enabled', True)
    return update_memory(memory_id, {'enabled': enabled}, project_path)
