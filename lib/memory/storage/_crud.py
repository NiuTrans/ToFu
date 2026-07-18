"""lib/memory/storage/_crud.py — List / query / CRUD operations.

Top-level API layer. Uses the SHARED ``_lock`` / ``_migrated_roots`` from
:mod:`_dirs` BY REFERENCE (imported as names bound to the same objects), the
per-file helpers from :mod:`_files`, and the path helpers from :mod:`_dirs`.
"""

import os
from datetime import datetime, timezone

from lib.log import get_logger

from ._dirs import (
    PROJECT_MEMORY_SUBDIR,
    MIN_DESCRIPTION_LENGTH,
    _get_global_memory_dir,
    _iter_roots,
    _lock,
    _migrate_one_root_globals,
    _migrated_roots,
    _server_global_memory_dir,
    resolve_target_dir,
)
from ._files import (
    _list_memories_in_dir,
    _make_memory_id,
    _write_memory_file,
)

logger = get_logger(__name__)


def list_all_memories(project_path=None, extra_paths=None):
    """List all global + project memories across the primary + extra roots.

    Project- and global-scoped memories are unioned across the primary
    ``project_path`` and every root in ``extra_paths`` (a multi-root
    session), de-duplicated by id with the primary root winning on a
    collision. With no ``extra_paths`` this is identical to the original
    single-root behaviour.
    """
    roots = _iter_roots(project_path, extra_paths)
    memories = []
    seen_ids = set()
    with _lock:
        # One-time idempotent migration of each root's legacy global dir into
        # the server store, so pre-existing globals become cross-project.
        for root in roots:
            if root in _migrated_roots:
                continue
            try:
                _migrate_one_root_globals(root)
            except Exception as e:
                logger.warning('[Memory] legacy global migration failed for '
                               '%s: %s', root, e)
            _migrated_roots.add(root)

        # Server-side global store first — canonical, scanned once regardless
        # of project, and wins on an id collision.
        for mem in _list_memories_in_dir(_server_global_memory_dir(),
                                         scope='global'):
            if mem['id'] in seen_ids:
                continue
            seen_ids.add(mem['id'])
            memories.append(mem)

        for root in roots:
            # Legacy per-root global dir (back-compat read; shadowed by id).
            global_dir = _get_global_memory_dir(root)
            scanned = (_list_memories_in_dir(global_dir, scope='global')
                       if global_dir else [])
            proj_dir = os.path.join(root, PROJECT_MEMORY_SUBDIR)
            scanned += _list_memories_in_dir(proj_dir, scope='project')
            for mem in scanned:
                if mem['id'] in seen_ids:
                    continue
                seen_ids.add(mem['id'])
                memories.append(mem)
    return memories


def list_memories(project_path=None, scope='all', extra_paths=None):
    """List memories, optionally filtered by scope."""
    all_memories = list_all_memories(project_path, extra_paths=extra_paths)
    if scope == 'global':
        return [s for s in all_memories if s['scope'] == 'global']
    elif scope == 'project':
        return [s for s in all_memories if s['scope'] == 'project']
    return all_memories


def get_memory(memory_id, project_path=None, extra_paths=None):
    """Get a single memory by ID. Returns memory dict or None."""
    for s in list_all_memories(project_path, extra_paths=extra_paths):
        if s['id'] == memory_id:
            return s
    return None


def get_enabled_memories(project_path=None, extra_paths=None):
    """Get only enabled memories."""
    return [s for s in list_all_memories(project_path, extra_paths=extra_paths)
            if s.get('enabled', True)]


def get_eligible_memories(project_path=None, extra_paths=None):
    """Get memories that are both enabled AND meet all runtime requirements."""
    return [
        s for s in get_enabled_memories(project_path, extra_paths=extra_paths)
        if s.get('eligible', True)
    ]


# ═══════════════════════════════════════════════════════
#  CRUD Operations
# ═══════════════════════════════════════════════════════

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


def update_memory(memory_id, updates, project_path=None, extra_paths=None):
    """Update an existing memory. Returns updated memory or None.

    The memory is located across the primary + extra roots and rewritten
    in place at its own ``filepath`` (so editing an extra-root memory
    stays in that root).
    """
    all_memories = list_all_memories(project_path, extra_paths=extra_paths)
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


def delete_memory(memory_id, project_path=None, extra_paths=None):
    """Delete a memory. Handles both flat ``.md`` files and package
    directories (``<id>/SKILL.md`` + references/scripts).

    The memory is located across the primary + extra roots.
    Returns True if deleted.
    """
    import shutil
    all_memories = list_all_memories(project_path, extra_paths=extra_paths)
    _roots = _iter_roots(project_path, extra_paths)
    # Global skill packages live in the server store, not under a project
    # root, so it must also be an allowed deletion prefix.
    _allowed = [os.path.realpath(r) for r in _roots]
    _allowed.append(os.path.realpath(_server_global_memory_dir()))
    for s in all_memories:
        if s['id'] != memory_id:
            continue
        try:
            if s.get('is_package') and s.get('package_dir'):
                pkg = s['package_dir']
                # Defence: only delete inside an allowed skills tree (project
                # roots + the server-side global store).
                pkg_real = os.path.realpath(pkg)
                if _allowed and not any(
                        pkg_real.startswith(a) for a in _allowed):
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


def merge_memories(memory_ids, name, description, body, tags=None, scope='project', project_path=None, extra_paths=None):
    """Merge multiple memories into one new consolidated memory, deleting the originals.

    Source memories are located across the primary + extra roots; the new
    consolidated memory is always written to the PRIMARY ``project_path``.
    """
    if not memory_ids or len(memory_ids) < 2:
        raise ValueError("merge_memories requires at least 2 memory IDs")

    # Resolve the CRUD collaborators THROUGH the package facade at call time so
    # a test's ``monkeypatch.setattr(lib.memory.storage, 'create_memory', …)``
    # (etc.) steers this orchestrator exactly as it did on the pre-split module.
    from lib.memory import storage as _facade
    _list_all = getattr(_facade, 'list_all_memories', list_all_memories)
    _create = getattr(_facade, 'create_memory', create_memory)
    _delete = getattr(_facade, 'delete_memory', delete_memory)

    all_memories = _list_all(project_path, extra_paths=extra_paths)
    mem_map = {s['id']: s for s in all_memories}
    missing = [sid for sid in memory_ids if sid not in mem_map]
    if missing:
        raise ValueError(f"Memories not found: {', '.join(missing)}")

    if tags is None:
        merged_tags = set()
        for sid in memory_ids:
            merged_tags.update(mem_map[sid].get('tags', []))
        tags = sorted(merged_tags)

    merged = _create(name=name, description=description, body=body,
                     tags=tags, scope=scope, project_path=project_path)

    deleted_ids = []
    failed_ids = []
    for sid in memory_ids:
        if _delete(sid, project_path, extra_paths=extra_paths):
            deleted_ids.append(sid)
        else:
            failed_ids.append(sid)
    if failed_ids:
        # A source that could not be deleted still lives ALONGSIDE the merged
        # copy → duplicated content. Surface it (delete_memory already logged
        # the OSError/guard reason) so the half-merge is not silent.
        logger.warning('[Memory] merge_memories: %d source memory(ies) could not '
                       'be deleted and remain as duplicates of the merged memory '
                       '%s: %s', len(failed_ids), merged['id'], ', '.join(failed_ids))

    return {'merged_memory': merged, 'deleted_ids': deleted_ids,
            'failed_ids': failed_ids}


def toggle_memory(memory_id, enabled=None, project_path=None, extra_paths=None):
    """Toggle a memory's enabled state."""
    if enabled is None:
        mem = get_memory(memory_id, project_path, extra_paths=extra_paths)
        if not mem:
            return None
        enabled = not mem.get('enabled', True)
    return update_memory(memory_id, {'enabled': enabled}, project_path,
                         extra_paths=extra_paths)
