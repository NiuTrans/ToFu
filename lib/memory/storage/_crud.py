"""lib/memory/storage/_crud.py — List / query / CRUD operations.

Top-level API layer. Uses the SHARED ``_lock`` / ``_migrated_roots`` from
:mod:`_dirs` BY REFERENCE (imported as names bound to the same objects), the
per-file helpers from :mod:`_files`, and the path helpers from :mod:`_dirs`.
"""

import os
import zlib
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone

from lib.json_store import locked_path
from lib.log import get_logger
from lib.memory.contracts import (
    MEMORY_BODY_MAX_CHARS,
    MEMORY_GENERATED_ID_MAX_BYTES,
    MEMORY_SEARCH_TOP_K_MAX,
    normalize_memory_payload,
    normalize_memory_tags,
    normalize_memory_updates,
    normalize_merge_memory_ids,
    truncate_utf8,
    validate_memory_id,
)

from ._dirs import (
    MIN_DESCRIPTION_LENGTH,
    _ensure_dir,
    _iter_memory_store_dirs,
    _lock,
    resolve_target_dir,
    run_storage_migrations,
)
from ._files import (
    _list_memories_in_dir,
    _make_memory_id,
    _memory_file_fingerprint,
    _memory_from_file,
    _memory_summary_from_id_in_dir,
    _validate_memory_record_view,
    _write_memory_file,
)

logger = get_logger(__name__)

_CREATE_MEMORY_ALLOCATION_LOCK = '.memory-create-allocation'
_MEMORY_MUTATION_LOCK_SHARDS = 16
_MEMORY_MUTATION_LOCK_RETRIES = 3


class MemoryRevisionConflict(ValueError):
    """A durable memory changed between discovery and mutation."""


def _revision_conflict(memory_id):
    return MemoryRevisionConflict(
        f"Memory '{memory_id}' changed; retry the operation")


def _memory_mutation_lock_path(filepath, package_dir=''):
    """Map one canonical record path onto a bounded directory-local shard."""
    canonical_path = os.path.normcase(
        os.path.realpath(os.path.abspath(filepath)))
    if package_dir:
        canonical_package_dir = os.path.normcase(
            os.path.realpath(os.path.abspath(package_dir)))
        lock_directory = os.path.dirname(canonical_package_dir)
    else:
        lock_directory = os.path.dirname(canonical_path)
    shard = zlib.crc32(os.fsencode(canonical_path)) % (
        _MEMORY_MUTATION_LOCK_SHARDS)
    return os.path.join(
        lock_directory,
        f'.memory-mutation-{shard:02d}',
    )


@contextmanager
def _memory_mutation_locks(record_paths):
    """Lock unique record shards in stable order to prevent merge deadlocks."""
    lock_paths = sorted({
        _memory_mutation_lock_path(filepath, package_dir)
        for filepath, package_dir in record_paths
        if filepath
    })
    with ExitStack() as stack:
        for lock_path in lock_paths:
            stack.enter_context(locked_path(lock_path))
        yield frozenset(lock_paths)


def list_all_memories(project_path=None, extra_paths=None, *,
                      include_body=True, record_view='complete'):
    """List all global + project memories across the primary + extra roots.

    Project- and global-scoped memories are unioned across the primary
    ``project_path`` and every root in ``extra_paths`` (a multi-root
    session), de-duplicated by id with the primary root winning on a
    collision. With no ``extra_paths`` this is identical to the original
    single-root behaviour.
    """
    _validate_memory_record_view(
        record_view, include_body=include_body)
    memories = []
    seen_ids = set()
    with _lock:
        # One-time idempotent storage migrations (legacy globals, pre-split
        # flat memories, server-store packages) so the scans below see the
        # post-split layout.
        run_storage_migrations(project_path, extra_paths)

        for directory, store_scope in _iter_memory_store_dirs(
                project_path, extra_paths):
            for mem in _list_memories_in_dir(
                    directory, scope=store_scope,
                    include_body=include_body,
                    record_view=record_view):
                if mem['id'] in seen_ids:
                    continue
                seen_ids.add(mem['id'])
                memories.append(mem)
    return memories


def list_memories(project_path=None, scope='all', extra_paths=None, *,
                  include_body=True):
    """List memories, optionally filtered by scope and body requirement."""
    all_memories = list_all_memories(
        project_path, extra_paths=extra_paths, include_body=include_body)
    if scope == 'global':
        return [s for s in all_memories if s['scope'] == 'global']
    elif scope == 'project':
        return [s for s in all_memories if s['scope'] == 'project']
    return all_memories


def _find_memory_summaries(memory_ids, project_path=None, extra_paths=None):
    """Resolve exact IDs by canonical path probes, never a corpus scan."""
    remaining_ids = list(dict.fromkeys(
        validate_memory_id(memory_id) for memory_id in memory_ids))
    found = {}
    with _lock:
        run_storage_migrations(project_path, extra_paths)
        for directory, store_scope in _iter_memory_store_dirs(
                project_path, extra_paths):
            if not remaining_ids:
                break
            if not os.path.isdir(directory):
                continue
            unresolved = []
            for memory_id in remaining_ids:
                memory = _memory_summary_from_id_in_dir(
                    directory, memory_id, scope=store_scope)
                if memory is None:
                    unresolved.append(memory_id)
                else:
                    found[memory_id] = memory
            remaining_ids = unresolved
    return found


def _find_memory_summary(memory_id, project_path=None, extra_paths=None):
    """Locate one canonical summary in O(visible roots), not O(memories)."""
    return _find_memory_summaries(
        [memory_id], project_path, extra_paths=extra_paths).get(memory_id)


def _read_summary_record(summary, *, include_body, body_char_limit=None):
    """Read the exact file represented by a repository-issued summary."""
    filepath = str(summary.get('filepath') or '')
    if not filepath or os.path.islink(filepath):
        return None
    is_package = bool(summary.get('is_package'))
    kwargs = {
        'scope': summary.get('scope', 'global'),
        'package_dir': summary.get('package_dir') or None,
        'memory_id_override': summary.get('id') if is_package else None,
        'include_body': include_body,
    }
    if body_char_limit is not None:
        kwargs['body_char_limit'] = body_char_limit
    return _memory_from_file(filepath, **kwargs)


def _refresh_memory_summary(summary):
    """Return a stable current summary plus its opaque filesystem revision."""
    filepath = str(summary.get('filepath') or '')
    try:
        revision_before = _memory_file_fingerprint(filepath)
        current = _read_summary_record(summary, include_body=False)
        revision_after = _memory_file_fingerprint(filepath)
    except OSError as exc:
        raise _revision_conflict(summary.get('id', '')) from exc
    if current is None or revision_before != revision_after:
        raise _revision_conflict(summary.get('id', ''))
    return current, revision_after


@contextmanager
def _locked_memory_revisions(
    memory_ids,
    project_path=None,
    extra_paths=None,
):
    """Yield stable summaries while holding every cooperating-writer shard.

    Discovery happens once before locking to identify candidate shards, then
    again under the ordered locks. If canonical precedence moved a record onto
    an unheld shard, retry from the new view rather than mutating it unlocked.
    """
    normalized_ids = list(dict.fromkeys(
        validate_memory_id(memory_id) for memory_id in memory_ids))
    for _attempt in range(_MEMORY_MUTATION_LOCK_RETRIES):
        candidates = _find_memory_summaries(
            normalized_ids,
            project_path,
            extra_paths=extra_paths,
        )
        candidate_paths = [
            (
                summary.get('filepath', ''),
                summary.get('package_dir', ''),
            )
            for summary in candidates.values()
        ]
        with _memory_mutation_locks(candidate_paths) as held_lock_paths:
            current = _find_memory_summaries(
                normalized_ids,
                project_path,
                extra_paths=extra_paths,
            )
            required_lock_paths = {
                _memory_mutation_lock_path(
                    summary['filepath'], summary.get('package_dir', ''))
                for summary in current.values()
            }
            if not required_lock_paths.issubset(held_lock_paths):
                continue
            stable = {
                memory_id: _refresh_memory_summary(summary)
                for memory_id, summary in current.items()
            }
            yield stable
            return
    conflict_id = normalized_ids[0] if normalized_ids else ''
    raise _revision_conflict(conflict_id)


def _hydrate_memory_at_revision(summary, revision):
    """Read one full durable record without accepting a mixed revision."""
    filepath = str(summary.get('filepath') or '')
    try:
        if _memory_file_fingerprint(filepath) != revision:
            raise _revision_conflict(summary.get('id', ''))
        memory = _read_summary_record(summary, include_body=True)
        if (memory is None
                or _memory_file_fingerprint(filepath) != revision):
            raise _revision_conflict(summary.get('id', ''))
        return memory
    except OSError as exc:
        raise _revision_conflict(summary.get('id', '')) from exc


def _locate_stable_memory(memory_id, project_path=None, extra_paths=None):
    summary = _find_memory_summary(
        memory_id, project_path, extra_paths=extra_paths)
    if summary is None:
        return None
    return _refresh_memory_summary(summary)


def _revision_is_current(memory, revision):
    try:
        return _memory_file_fingerprint(memory['filepath']) == revision
    except (KeyError, OSError):
        return False


def get_memory(memory_id, project_path=None, extra_paths=None):
    """Get one full record via a metadata lookup; fail soft on races."""
    try:
        memory_id = validate_memory_id(memory_id)
    except ValueError:
        return None
    for _attempt in range(2):
        try:
            located = _locate_stable_memory(
                memory_id, project_path, extra_paths=extra_paths)
            if located is None:
                return None
            summary, revision = located
            return _hydrate_memory_at_revision(summary, revision)
        except MemoryRevisionConflict:
            continue
    return None


def get_enabled_memories(project_path=None, extra_paths=None, *,
                         include_body=True, record_view='complete'):
    """Get only enabled memories, optionally as metadata-only records."""
    return [
        memory
        for memory in list_all_memories(
            project_path,
            extra_paths=extra_paths,
            include_body=include_body,
            record_view=record_view,
        )
        if memory.get('enabled', True)
    ]


def get_eligible_memories(project_path=None, extra_paths=None,
                          include_packages=False, *, include_body=True,
                          record_view='complete'):
    """Get memories that are both enabled AND meet all runtime requirements.

    SKILL PACKAGES are excluded by default: they are a different noun
    (user-installed instruction guides) with their own channel — the
    ``<available_skills>`` index + ``load_skill`` — so the memory
    prefetch/search/injection corpus stays pure MEMORY and packages stop
    competing with experience notes for injection slots.
    """
    return [
        s for s in get_enabled_memories(
            project_path,
            extra_paths=extra_paths,
            include_body=include_body,
            record_view=record_view,
        )
        if s.get('eligible', True)
        and (include_packages or not s.get('is_package'))
    ]


def _validated_body_char_limit(body_char_limit):
    if (isinstance(body_char_limit, bool)
            or not isinstance(body_char_limit, int)
            or not 0 <= body_char_limit <= MEMORY_BODY_MAX_CHARS):
        raise ValueError(
            'body_char_limit must be an integer between 0 and '
            f'{MEMORY_BODY_MAX_CHARS:,}')
    return body_char_limit


def _hydrate_memory_summary(summary, *, body_char_limit):
    """Hydrate one repository-issued flat-memory summary with a body prefix."""
    filepath = str(summary.get('filepath') or '')
    if (not filepath or summary.get('is_package')
            or os.path.islink(filepath)):
        return None
    memory = _read_summary_record(
        summary,
        include_body=True,
        body_char_limit=body_char_limit,
    )
    if (not memory or not memory.get('enabled', True)
            or not memory.get('eligible', True)
            or memory.get('is_package')):
        return None
    return memory


def iter_eligible_memories(
    project_path=None,
    extra_paths=None,
    *,
    body_char_limit,
    scope=None,
) -> Iterator[dict]:
    """Stream bounded-body memory records from one metadata-only snapshot.

    The list phase preserves canonical root precedence and ID de-duplication.
    Each body is then loaded only as the scorer requests it, so a caller never
    materializes every durable body at once.
    """
    body_char_limit = _validated_body_char_limit(body_char_limit)
    if scope not in (None, 'global', 'project'):
        raise ValueError("scope must be 'global', 'project', or None")
    summaries = get_eligible_memories(
        project_path,
        extra_paths=extra_paths,
        include_body=False,
        record_view='retrieval',
    )
    for summary in summaries:
        if scope is not None and summary.get('scope') != scope:
            continue
        memory = _hydrate_memory_summary(
            summary, body_char_limit=body_char_limit)
        if memory is not None:
            yield memory


def load_eligible_memories(
    memory_ids,
    project_path=None,
    extra_paths=None,
    *,
    body_char_limit,
) -> list[dict]:
    """Load bounded bodies for at most 50 owner-visible memory IDs."""
    if (not isinstance(memory_ids, Sequence)
            or isinstance(memory_ids, (str, bytes, bytearray))):
        raise ValueError('memory_ids must be an array of memory IDs')
    if len(memory_ids) > MEMORY_SEARCH_TOP_K_MAX:
        raise ValueError(
            f'memory_ids accepts at most {MEMORY_SEARCH_TOP_K_MAX} IDs')
    body_char_limit = _validated_body_char_limit(body_char_limit)
    requested_ids = []
    seen_ids = set()
    for memory_id in memory_ids:
        memory_id = validate_memory_id(memory_id)
        if memory_id not in seen_ids:
            seen_ids.add(memory_id)
            requested_ids.append(memory_id)

    # Callers already paid for corpus ranking and request at most a bounded ID
    # set. Rebuilding every summary here made two selected prefetch records
    # retain the entire corpus a second time. Exact canonical probes preserve
    # root precedence while scaling with requested evidence, not corpus size.
    summaries_by_id = _find_memory_summaries(
        requested_ids,
        project_path,
        extra_paths=extra_paths,
    )
    loaded = []
    for memory_id in requested_ids:
        summary = summaries_by_id.get(memory_id)
        if (summary is None
                or not summary.get('enabled', True)
                or not summary.get('eligible', True)
                or summary.get('is_package')):
            continue
        memory = _hydrate_memory_summary(
            summary, body_char_limit=body_char_limit)
        if memory is not None:
            loaded.append(memory)
    return loaded


# ═══════════════════════════════════════════════════════
#  CRUD Operations
# ═══════════════════════════════════════════════════════

def _guard_not_package(target, memory_id, op):
    """Refuse model-side CRUD against an installed SKILL PACKAGE.

    Skill packages are USER-installed capability packs — install /
    uninstall / enable-toggle are user-only actions (Settings → Skills).
    The model's memory tools must never rewrite, merge, or delete one
    (a skill is a different noun from a memory).
    """
    if target and target.get('is_package'):
        raise ValueError(
            f"Cannot {op} '{memory_id}': it is an installed skill package, "
            f"not a memory. Skill packages are managed by the user in the "
            f"Settings → Skills tab; use load_skill to load one.")


def _collision_memory_id(base_memory_id, counter):
    """Append a numeric suffix without exceeding the generated-ID budget."""
    suffix = f'_{counter}'
    prefix_budget = MEMORY_GENERATED_ID_MAX_BYTES - len(
        suffix.encode('utf-8'))
    prefix = truncate_utf8(base_memory_id, prefix_budget).rstrip('_-')
    return f'{prefix}{suffix}'


def _memory_id_is_allocated(dirpath, memory_id):
    """Treat flat files, packages, and broken symlinks as occupied names."""
    return (
        os.path.lexists(os.path.join(dirpath, f'{memory_id}.md'))
        or os.path.lexists(os.path.join(dirpath, memory_id))
    )


def _allocate_memory_path(dirpath, base_memory_id):
    """Choose the lowest free ID using at most one directory snapshot."""
    if not _memory_id_is_allocated(dirpath, base_memory_id):
        return base_memory_id, os.path.join(dirpath, f'{base_memory_id}.md')

    entries = set(os.listdir(dirpath))
    # Every occupied candidate consumes at least one distinct directory entry,
    # so len(entries) + 1 attempts is a finite proof that a free suffix exists.
    for counter in range(1, len(entries) + 2):
        candidate = _collision_memory_id(base_memory_id, counter)
        if candidate in entries or f'{candidate}.md' in entries:
            continue
        return candidate, os.path.join(dirpath, f'{candidate}.md')
    raise RuntimeError('memory ID allocation exhausted unexpectedly')


def create_memory(name, description='', body='', tags=None, scope='global', project_path=None):
    """Create one bounded memory atomically; return its storage record."""
    name, description, body, tags, scope = normalize_memory_payload(
        name=name,
        description=description,
        body=body,
        tags=tags,
        scope=scope,
    )
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
        'enabled': True, 'tags': tags,
        'requires_bins': [], 'requires_env': [],
        'created': now, 'updated': now, 'body': body, 'scope': scope,
    }

    dirpath = resolve_target_dir(scope, project_path)

    _ensure_dir(dirpath)
    allocation_lock_path = os.path.join(
        dirpath, _CREATE_MEMORY_ALLOCATION_LOCK)
    # One stable sidecar lock serializes the select+publish boundary across
    # threads and POSIX processes.  The final markdown file is still published
    # by write_text_atomic, so readers never observe a partially written body.
    with locked_path(allocation_lock_path):
        mem['id'], filepath = _allocate_memory_path(dirpath, memory_id)
        mem['updated'] = _write_memory_file(filepath, mem)
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
    memory_id = validate_memory_id(memory_id)
    updates = normalize_memory_updates(updates)
    with _locked_memory_revisions(
            [memory_id], project_path, extra_paths=extra_paths) as records:
        located = records.get(memory_id)
        if located is None:
            return None
        summary, revision = located
        _guard_not_package(summary, memory_id, 'update')
        target = _hydrate_memory_at_revision(summary, revision)
        _guard_not_package(target, memory_id, 'update')
        for key in ('name', 'description', 'body', 'tags', 'enabled',
                    'requires_bins', 'requires_env'):
            if key in updates:
                target[key] = updates[key]
        if not _revision_is_current(target, revision):
            raise _revision_conflict(memory_id)
        target['updated'] = _write_memory_file(target['filepath'], target)
        return target


def delete_memory(memory_id, project_path=None, extra_paths=None):
    """Delete a flat memory file.

    Skill packages are NOT deletable here — they are a different noun
    (user-installed); see :func:`_guard_not_package`. The Settings →
    Skills tab uninstalls packages via the skills API's own path.

    The memory is located across the primary + extra roots.
    Returns True if deleted.
    """
    memory_id = validate_memory_id(memory_id)
    with _locked_memory_revisions(
            [memory_id], project_path, extra_paths=extra_paths) as records:
        located = records.get(memory_id)
        if located is None:
            return False
        target, revision = located
        _guard_not_package(target, memory_id, 'delete')
        if not _revision_is_current(target, revision):
            raise _revision_conflict(memory_id)
        try:
            os.remove(target['filepath'])
            return True
        except OSError:
            logger.warning('Failed to delete memory id=%s', memory_id,
                           exc_info=True)
            return False


def clear_memories(project_path=None, extra_paths=None, *, dry_run=False):
    """Delete every visible flat memory while preserving skill packages.

    This operation is intentionally scoped to the same global + project view
    returned by :func:`list_all_memories`.  It is exposed only through the
    authenticated Settings flow for personal/private deployments.
    """
    memories = [
        memory
        for memory in list_all_memories(
            project_path,
            extra_paths=extra_paths,
            include_body=False,
        )
        if not memory.get('is_package')
    ]
    counts = {
        'total': len(memories),
        'global': sum(1 for m in memories if m.get('scope') == 'global'),
        'project': sum(1 for m in memories if m.get('scope') == 'project'),
    }
    if dry_run:
        return {**counts, 'deleted_ids': [], 'failed_ids': []}
    deleted_ids = []
    failed_ids = []
    for memory in memories:
        memory_id = memory.get('id', '')
        try:
            with _locked_memory_revisions(
                    [memory_id], project_path,
                    extra_paths=extra_paths) as records:
                located = records.get(memory_id)
                if located is None:
                    failed_ids.append(memory_id)
                    continue
                current, revision = located
                if (current.get('is_package')
                        or not _revision_is_current(current, revision)):
                    failed_ids.append(memory_id)
                    continue
                os.remove(current['filepath'])
                deleted_ids.append(memory_id)
        except (MemoryRevisionConflict, OSError):
            logger.warning('Failed to clear memory id=%s', memory_id,
                           exc_info=True)
            failed_ids.append(memory_id)
    return {**counts, 'deleted_ids': deleted_ids, 'failed_ids': failed_ids}


def merge_memories(memory_ids, name, description, body, tags=None, scope='project', project_path=None, extra_paths=None):
    """Merge multiple memories into one new consolidated memory, deleting the originals.

    Source memories are located across the primary + extra roots; the new
    consolidated memory is always written to the PRIMARY ``project_path``.
    """
    memory_ids = normalize_merge_memory_ids(memory_ids)
    name, description, body, tags, scope = normalize_memory_payload(
        name=name,
        description=description,
        body=body,
        tags=tags,
        scope=scope,
        allow_tags_none=True,
    )

    with _locked_memory_revisions(
            memory_ids, project_path, extra_paths=extra_paths) as records:
        missing = [sid for sid in memory_ids if sid not in records]
        if missing:
            raise ValueError(f"Memories not found: {', '.join(missing)}")

        stable_memories = {}
        source_revisions = {}
        for sid in memory_ids:
            memory, revision = records[sid]
            _guard_not_package(memory, sid, 'merge')
            stable_memories[sid] = memory
            source_revisions[sid] = revision

        if tags is None:
            merged_tags = set()
            for sid in memory_ids:
                source_tags = stable_memories[sid].get('tags', [])
                # A legacy hand-written ``tags: solo`` frontmatter value can
                # parse as one string. Treat it as one tag, never as an
                # iterable of characters during the union.
                if isinstance(source_tags, str):
                    source_tags = [source_tags]
                merged_tags.update(source_tags or [])
            tags = sorted(merged_tags)
            # Legacy/user-authored source files can predate the bounded write
            # contract. Validate their derived union before creating or
            # deleting anything; never silently trim durable metadata.
            tags = normalize_memory_tags(tags)

        merged = create_memory(
            name=name,
            description=description,
            body=body,
            tags=tags,
            scope=scope,
            project_path=project_path,
        )

        deleted_ids = []
        failed_ids = []
        for sid in memory_ids:
            source = stable_memories[sid]
            if not _revision_is_current(source, source_revisions[sid]):
                failed_ids.append(sid)
                continue
            try:
                os.remove(source['filepath'])
                deleted_ids.append(sid)
            except OSError:
                logger.warning('Failed to delete merged source id=%s', sid,
                               exc_info=True)
                failed_ids.append(sid)
        if failed_ids:
            # A source that could not be deleted still lives ALONGSIDE the
            # merged copy → duplicated content. Surface changed revisions and
            # logged I/O failures so the half-merge is never silent.
            logger.warning(
                '[Memory] merge_memories: %d source memory(ies) could not be '
                'deleted and remain as duplicates of the merged memory %s: %s',
                len(failed_ids), merged['id'], ', '.join(failed_ids))

        return {
            'merged_memory': merged,
            'deleted_ids': deleted_ids,
            'failed_ids': failed_ids,
        }


def toggle_memory(memory_id, enabled=None, project_path=None, extra_paths=None):
    """Toggle a memory's enabled state.

    Deliberately does NOT route through :func:`update_memory`: that one is
    package-guarded (model CRUD safety), while enable/disable is ALSO the
    Settings → Skills enable toggle for packages — a user-only API action
    that must keep working for skill packages.
    """
    memory_id = validate_memory_id(memory_id)
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError('enabled must be a boolean or null')
    with _locked_memory_revisions(
            [memory_id], project_path, extra_paths=extra_paths) as records:
        located = records.get(memory_id)
        if located is None:
            return None
        summary, revision = located
        mem = _hydrate_memory_at_revision(summary, revision)
        if enabled is None:
            enabled = not mem.get('enabled', True)
        mem['enabled'] = enabled
        if not _revision_is_current(mem, revision):
            raise _revision_conflict(memory_id)
        mem['updated'] = _write_memory_file(mem['filepath'], mem)
        return mem
