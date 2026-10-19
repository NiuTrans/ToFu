"""Modification history, undo/redo, and session tracking.

2026-04-22 — Per-session-dir storage (multi-project concurrency safe)
────────────────────────────────────────────────────────────────────────
Previously this module kept a *single* global list ``_state['modifications']``.
When the UI switched active projects, ``_start_new_session()`` wiped that
list and repopulated it from the newly-active project's file — while
background tasks belonging to the *old* project were still running and
calling ``_record_modification()``.  The result: those background writes
would flush the wrong (swapped-in) list into the old project's file,
silently clobbering history.

The robust fix below makes **disk the source of truth per session_dir**
and keeps an in-memory cache keyed by ``session_dir`` instead of a single
global list.  Every mutation goes through ``_locked_rmw`` (read-modify-
write under the lock) so concurrent tasks operating on *different*
roots cannot interfere with each other, and concurrent tasks on the
*same* root still serialise correctly.

``_state['modifications']`` / ``_state['sessionId']`` remain as a
*read-only* projection of the currently-active primary root for the
sake of ``get_state()`` / UI badges; nothing writes through them.
"""
import base64
from collections import OrderedDict
import hashlib
from functools import wraps
import re
import json
import os
import stat
import sys
import tempfile
import time
import uuid

from lib.log import get_logger
from lib.project_mod import undo_payloads
from lib.tools.execution_timing import timing_phase
from lib.project_mod.config import (
    SESSIONS_DIR,
    MAX_FILE_SIZE,
    _lock,
    _roots,
    _state,
)
from runtime_guards import resolve_resource_budget
from lib.project_mod.snapshot_content import UNDO_BATCH_CONTENT_BYTES
from lib.project_mod.session_lock import locked_session, session_state, after_session_release

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  Per-session-dir in-memory cache
# ═══════════════════════════════════════════════════════════════════
# Maps absolute session_dir → list[mod dict]. Disk remains authoritative, so an
# LRU eviction only trades a later read for bounded resident memory.
_MODS_CACHE_CAPACITY = resolve_resource_budget(
    'TOFU_PROJECT_UNDO_CACHE_CAPACITY', maximum=2048)
# Reuse the launch-probed/overridden session budget: each unit grants 256 KiB
# of resident history (16 MiB at the lean 64-session default, at most 512 MiB).
# Sessions share the total byte budget through LRU eviction; no fixed fraction
# excludes an active metadata index that fits the machine-resolved budget.
_MODS_CACHE_MAX_BYTES = _MODS_CACHE_CAPACITY * 256 * 1024
_MODS_CACHE_MAX_SESSION_BYTES = _MODS_CACHE_MAX_BYTES
_mods_cache: OrderedDict[str, list] = OrderedDict()
_mods_cache_identity = {}
_mods_cache_bytes = {}


def _mods_cache_weight(session_dir, mods, identity):
    """Conservative resident weight, stopping as soon as admission is impossible.

    Count shared objects repeatedly rather than allocating an unbounded seen-set.
    Only recursion depth is transient; a shallow primary-view list is reserved
    even for inactive entries so switching the active project cannot grow memory.
    This traversal runs under session admission, never the global cache lock.
    """
    limit = min(_MODS_CACHE_MAX_SESSION_BYTES, _MODS_CACHE_MAX_BYTES)
    size = 512 + sys.getsizeof(mods)  # mapping metadata and primary list slots

    def visit(value):
        nonlocal size
        size += sys.getsizeof(value)
        if size > limit:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                visit(key)
                visit(child)
                if size > limit:
                    break
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)
                if size > limit:
                    break

    try:
        for value in (session_dir, identity, mods):
            visit(value)
            if size > limit:
                break
    except RecursionError:
        # Cyclic/deep data cannot be a useful bounded reconstructible cache.
        return limit + 1
    return size


def _journal_identity(session_dir):
    """Atomic replacement identity, not a TTL; disk remains authoritative."""
    try:
        metadata = os.stat(os.path.join(session_dir, 'modifications.json'), follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns)

# ── Live-change listeners ─────────────────────────────────────────────
# Presentation-only channel: the task engine subscribes
# (``commit_round._derive.note_live_file_change``, registered from
# ``lib.tasks_pkg.handlers.project``) so a RUNNING turn's fileChanges card
# grows as edits land instead of appearing only at settlement.  The journal
# stays the sole authority — listeners observe, never mutate.
_live_change_listeners: list = []


def register_live_change_listener(fn):
    """Subscribe ``fn(task_id, entry)`` to journaled file changes. Idempotent."""
    if fn not in _live_change_listeners:
        _live_change_listeners.append(fn)


def _default_live_action(mod_type, existed):
    """Map a journal record to the settlement action vocabulary
    (``commit_round._derive.derive_round_modified_files``)."""
    if mod_type == 'write_file':
        return 'written' if existed else 'created'
    if mod_type in ('apply_diff', 'apply_diffs'):
        return 'patched'
    if mod_type in ('insert_content', 'insert_contents'):
        return 'inserted'
    if mod_type == 'run_command':
        return 'modified' if existed else 'created'
    return mod_type


def _sync_journal_directory(directory):
    """Persist journal name changes; an unsupported flush is a visible failure."""
    descriptor = os.open(directory or '.', os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


_JSON_WRITE_BUFFER_BYTES = 64 * 1024


def _write_compact_json(handle, data):
    """Encode one metadata row in C, then emit exact bounded ASCII batches."""
    encoder = json.JSONEncoder(ensure_ascii=True, separators=(',', ':'))
    def metadata_tokens():
        yield '{"modifications":['
        for index, record in enumerate(data['modifications']):
            if index:
                yield ','
            yield encoder.encode(record)
        yield ']}'
    tokens = (metadata_tokens() if isinstance(data, dict) and set(data) == {'modifications'}
              else encoder.iterencode(data))
    pieces = []
    size = written = 0
    for token in tokens:
        offset = 0
        while offset < len(token):
            end = min(len(token), offset + _JSON_WRITE_BUFFER_BYTES - size)
            pieces.append(token[offset:end])
            size += end - offset
            offset = end
            if size == _JSON_WRITE_BUFFER_BYTES:
                handle.write(''.join(pieces))
                written += size
                pieces.clear()
                size = 0
    if pieces:
        handle.write(''.join(pieces))
        written += size
    return written


def _atomic_json_write(filepath, data):
    """Write JSON data to file atomically (write to temp, then rename).

    Prevents file corruption if the process is killed mid-write.
    os.replace() is atomic on POSIX when src and dst are on the same filesystem.
    """
    dir_name = os.path.dirname(filepath)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp', prefix='.modifications_')
    try:
        with os.fdopen(fd, 'w', encoding='ascii', buffering=_JSON_WRITE_BUFFER_BYTES) as f:
            written_bytes = _write_compact_json(f, data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
        _sync_journal_directory(dir_name)
        return written_bytes
    except BaseException:
        # Clean up temp file on any failure (including KeyboardInterrupt)
        try:
            os.unlink(tmp_path)
        except OSError as _e_audit:
            logger.debug('[modifications] _atomic_json_write caught %s: %s', type(_e_audit).__name__, _e_audit)
            pass
        raise


def _remember_project_root(session_dir, base_path):
    """Persist the server-resolved root independently of pending changes."""
    marker_file = os.path.join(session_dir, 'project.json')
    normalized = os.path.abspath(base_path)
    try:
        if os.path.isfile(marker_file):
            with open(marker_file) as f:
                if json.load(f).get('basePath') == normalized:
                    return
        _atomic_json_write(marker_file, {'basePath': normalized})
    except Exception as e:
        # A modification record also carries basePath, so a marker failure
        # must not turn an otherwise valid project write into an exception.
        logger.debug('[Modifications] project marker write failed: %s', e)


def _nudge_vscode(base_path, filepath):
    """Notify timestamp-polling editors without reopening unanchored paths."""
    from lib.project_mod import undo_filesystem
    try:
        undo_filesystem.touch_file(base_path, filepath)
    except (OSError, ValueError, NotImplementedError) as exc:
        logger.debug('Failed to bump mtime for VS Code watcher on %s: %s', filepath, exc)


def _ensure_durable_session_directory(directory):
    """Create ancestors in publication order, flushing each parent before descent.

    Retry must flush the deepest existing ancestor too: a previous mkdir may
    have succeeded while its parent flush failed. Do not create descendants
    until that uncertainty is resolved. The iterative walk is bounded by the
    requested path depth and holds no directory descriptors between flushes.
    """
    pending = []
    current = os.path.abspath(directory)
    while not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current:
            raise OSError('No existing ancestor for session directory')
        pending.append(current)
        current = parent
    _sync_journal_directory(os.path.dirname(current))
    for current in reversed(pending):
        try:
            os.mkdir(current)
        except FileExistsError:
            if not os.path.isdir(current):
                raise
        _sync_journal_directory(os.path.dirname(current))


def _get_session_dir(base_path, *, create=True):
    """Get session directory for storing modification history."""
    if not base_path:
        return None
    session_id = hashlib.md5(base_path.encode()).hexdigest()[:12]
    session_dir = os.path.join(SESSIONS_DIR, session_id)
    # The outer admission already initialized and flushed this directory.
    # Nested recovery/recording must not re-fsync its ancestors at every layer.
    if create and session_state(session_dir) is None:
        _ensure_durable_session_directory(session_dir)
        _remember_project_root(session_dir, base_path)
    return session_dir


def resolve_base_path(task_id=None, conv_id=None):
    """Find the project ``base_path`` that recorded a given task/conversation.

    Scans every persisted session's modifications.json for a record whose
    ``taskId`` (preferred) or ``convId`` matches, and returns the
    ``basePath`` stored on that record.  This lets undo target the correct
    project even when the caller can't supply an explicit ``projectPath``
    and the globally-active project (``_state['path']``) points elsewhere
    — the exact situation that breaks undo when multiple conversations
    edit different projects concurrently.

    Returns the absolute base_path str, or ``None`` if no matching record
    is found (e.g. legacy records written before ``basePath`` tagging).
    """
    if not task_id and not conv_id:
        return None
    try:
        names = os.listdir(SESSIONS_DIR)
    except OSError as e:
        logger.debug('[Modifications] resolve_base_path: cannot list %s: %s',
                     SESSIONS_DIR, e)
        return None
    history_roots = []
    for name in names:
        session_dir = os.path.join(SESSIONS_DIR, name)
        marker_file = os.path.join(session_dir, 'project.json')
        try:
            with open(marker_file) as f:
                marker = json.load(f)
                marker_root = marker.get('basePath') if isinstance(marker, dict) else None
                if not isinstance(marker_root, str):
                    marker_root = ''
            if marker_root:
                history_roots.append(marker_root)
        except (OSError, TypeError, ValueError):
            pass
        # A crash can leave task attribution only in the prepared intent.
        # Resolve that root without recovering or mutating unrelated projects.
        if os.path.isdir(os.path.join(session_dir, 'write-intents-v1')):
            try:
                from lib.project_mod.write_intents import locked_intents
                with locked_intents(session_dir) as store:
                    for pending in store.read():
                        matches = (pending.get('taskId') == task_id if task_id
                                   else pending.get('convId') == conv_id)
                        expected_session = hashlib.md5(pending['basePath'].encode()).hexdigest()[:12]
                        if matches and name == expected_session:
                            return pending['basePath']
            except (OSError, ValueError) as exc:
                logger.debug('Prepared root lookup skipped %s: %s', name, exc)
        session_file = os.path.join(session_dir, 'modifications.json')
        if not os.path.isfile(session_file):
            continue
        try:
            mods = _load_from_disk(session_dir)
        except (OSError, ValueError) as e:
            logger.debug('[Modifications] resolve_base_path: skip %s: %s', name, e)
            continue
        for m in mods:
            if task_id and m.get('taskId') == task_id and m.get('basePath'):
                return m['basePath']
            if conv_id and not task_id and m.get('convId') == conv_id and m.get('basePath'):
                return m['basePath']
    # Undo removes completed task records from modifications.json. The root
    # marker plus append-only file history keeps redo resolvable after reload
    # without accepting a filesystem path from the browser.
    if history_roots:
        try:
            from lib import file_history

            for base_path in dict.fromkeys(history_roots):
                snapshot_id = file_history.find_latest_snapshot_id(
                    base_path,
                    task_id=task_id or None,
                    conv_id=conv_id if not task_id else None,
                )
                if snapshot_id:
                    return base_path
        except Exception as e:
            logger.debug('[Modifications] history path resolution failed: %s', e)
    return None


# Only remove .tmp files older than this — crash artifacts are much
# older; in-flight atomic writes are sub-second.
_STALE_TMP_AGE_SECONDS = 60.0


def _clean_stale_tmp(session_dir):
    """Remove leftover atomic-write temp files from previous crashes.

    Safety rails:
      - Runs only on a bounded-cache cold load.
      - Deletes only files older than :data:`_STALE_TMP_AGE_SECONDS` so a
        concurrent in-flight :func:`_atomic_json_write` on the same
        session_dir cannot have its fresh .tmp file deleted before
        ``os.replace`` runs.

    Caller must hold session admission so it cannot race a journal writer.
    """
    try:
        now = time.time()
        for name in os.listdir(session_dir):
            if not (name.startswith('.modifications_') and name.endswith('.tmp')):
                continue
            stale = os.path.join(session_dir, name)
            try:
                age = now - os.path.getmtime(stale)
                if age < _STALE_TMP_AGE_SECONDS:
                    continue  # likely in-flight, don't touch
                os.unlink(stale)
                logger.debug('Cleaned up stale temp file (age=%.0fs): %s', age, name)
            except OSError as e:
                logger.debug('Could not remove stale temp %s: %s', name, e)
    except OSError as e:
        logger.debug('Failed to list session dir for cleanup %s: %s', session_dir, e)


def _load_from_disk(session_dir):
    """Load authoritative history; only a missing file means empty history.

    Read failures and malformed records remain visible and never rename,
    replace or silently discard durable evidence. A rejected load must not
    populate the cache, so later mutations cannot overwrite it with [] plus
    a newly appended record.
    """
    session_file = os.path.join(session_dir, 'modifications.json')
    try:
        size = os.stat(session_file).st_size
        with timing_phase('undo_index_read', bytes_read=size):
            with open(session_file) as handle:
                data = json.load(handle)
    except FileNotFoundError:
        return []
    if not isinstance(data, dict) or not isinstance(data.get('modifications'), list):
        raise ValueError('Invalid modification journal envelope')
    records = data['modifications']
    for record in records:
        if (not isinstance(record, dict)
                or not isinstance(record.get('type'), str) or not record['type']
                or not isinstance(record.get('path'), str) or not record['path']):
            raise ValueError('Invalid modification journal record')
        for name in ('basePath', 'taskId', 'convId', 'changeId'):
            if record.get(name) is not None and not isinstance(record[name], str):
                raise ValueError('Invalid modification journal attribution')
    for record in records:
        for field in undo_payloads.FIELDS:
            if undo_payloads.is_reference(record.get(field)):
                undo_payloads.validate_reference(record[field])
    return records


def _publish_cache(session_dir, mods, identity):
    """Admit a confirmed snapshot by bytes/count; never truncate durable history."""
    weight = _mods_cache_weight(session_dir, mods, identity)
    with _lock:
        _invalidate_cache(session_dir)
        if weight > min(_MODS_CACHE_MAX_SESSION_BYTES, _MODS_CACHE_MAX_BYTES):
            return
        _mods_cache[session_dir] = mods
        _mods_cache_identity[session_dir] = identity
        _mods_cache_bytes[session_dir] = weight
        _mods_cache.move_to_end(session_dir)
        total = sum(_mods_cache_bytes[key] for key in _mods_cache)
        while len(_mods_cache) > _MODS_CACHE_CAPACITY or total > _MODS_CACHE_MAX_BYTES:
            evicted_dir = next(iter(_mods_cache))
            total -= _mods_cache_bytes[evicted_dir]
            _invalidate_cache(evicted_dir)
        if _state.get('sessionId') == session_dir:
            _state['modifications'] = list(mods)


def _invalidate_cache(session_dir):
    with _lock:
        _mods_cache.pop(session_dir, None)
        _mods_cache_identity.pop(session_dir, None)
        _mods_cache_bytes.pop(session_dir, None)
        if _state.get('sessionId') == session_dir:
            _state['modifications'] = []


def _cache_get(session_dir):
    """Read a session snapshot without holding the global cache lock over I/O.

    Do not hold ``_lock`` on entry: admission always precedes the cache lock.
    Returned records are read-only; journal mutations use ``_locked_rmw``.
    """
    with locked_session(session_dir):
        identity = _journal_identity(session_dir)
        with _lock:
            cached = _mods_cache.get(session_dir)
            if cached is not None and _mods_cache_identity.get(session_dir) == identity:
                _mods_cache.move_to_end(session_dir)
                return cached
        _invalidate_cache(session_dir)
        _clean_stale_tmp(session_dir)
        mods = _load_from_disk(session_dir)
        if _journal_identity(session_dir) != identity:
            raise OSError('Modification journal changed while reading')
        _publish_cache(session_dir, mods, identity)
        try:
            with timing_phase('undo_orphan_cleanup'):
                undo_payloads.reclaim_orphans(session_dir, mods,
                    max_entries=_MODS_CACHE_CAPACITY * 8)
        except OSError:
            logger.warning('Undo crash-artifact cleanup deferred; journal remains authoritative')
        return mods


def modification_cache_snapshot() -> dict[str, int]:
    """Return bounded, content-free cache telemetry for diagnostics/tests."""
    with _lock:
        return {
            'entries': len(_mods_cache),
            'capacity': _MODS_CACHE_CAPACITY,
            'pendingRecords': sum(len(mods) for mods in _mods_cache.values()),
        }


def _flush_to_disk(session_dir, mods):
    """Atomically persist ``mods`` to ``session_dir``'s modifications.json.

    Removes the file when the list is empty. Caller holds session admission.
    """
    session_file = os.path.join(session_dir, 'modifications.json')
    try:
        if mods:
            with timing_phase('undo_content_compaction', records=len(mods)):
                compact = undo_payloads.compact_records(session_dir, mods,
                    buffer_budget=_MODS_CACHE_MAX_SESSION_BYTES)
            with timing_phase('undo_index_write', records=len(compact)) as measurement:
                written = _atomic_json_write(session_file, {'modifications': compact})
                if measurement is not None and type(written) is int:
                    measurement['bytes_written'] += written
            mods[:] = compact
        else:
            if os.path.exists(session_file):
                os.remove(session_file)
                _sync_journal_directory(session_dir)
    except Exception as e:
        logger.error('[Modifications] failed to save %s: %s',
                     session_file, e, exc_info=True)
        raise


def _sync_primary_view(session_dir, mods):
    """Mirror the cached mods for ``session_dir`` into ``_state`` so that
    ``get_state()`` / the UI badge reflects the currently-active primary
    root.  ``_state['modifications']`` is intentionally **read-only** from
    the rest of the codebase — nothing should ever mutate it directly.
    Caller must hold ``_lock``.
    """
    # Only an admitted cache entry may stay resident in the active projection.
    # Oversized history remains available from the authoritative journal API.
    _state['sessionId'] = session_dir
    _state['modifications'] = list(mods) if _mods_cache.get(session_dir) is mods else []


def _cached_current(session_dir):
    """Return a private copy of the cached journal iff it still matches disk.

    Caller must hold session admission. The identity check under that
    admission is authoritative: another in-process writer cannot interleave,
    and a cross-process or external writer changes the journal identity, so a
    stale cache never substitutes for durable history. Returns ``None`` when
    there is no validated snapshot, forcing an authoritative disk read.
    """
    identity = _journal_identity(session_dir)
    with _lock:
        cached = _mods_cache.get(session_dir)
        if cached is not None and _mods_cache_identity.get(session_dir) == identity:
            return list(cached)
    return None


def _locked_rmw(session_dir, mutator):
    """Publish one journal mutation under process and thread admission.

    The mutation input is, in priority order: a fresh private journal prepared
    under this same admission, else an identity-validated cached snapshot
    (copy), else an authoritative disk read. Reusing the validated cache keeps
    a repeated writer's critical section off a full journal re-read — the main
    driver of ``journal-busy`` on large journals / slow filesystems. The
    mutator receives a private list and may return a replacement list. Only a
    confirmed durable flush updates the projection; any uncertain flush
    invalidates it, so a torn/corrupt journal never persists as a cache entry.
    """
    with locked_session(session_dir) as state:
        mods = state.pop('preparedJournal', None)
        if mods is None:
            mods = _cached_current(session_dir)
        if mods is None:
            _invalidate_cache(session_dir)
            _clean_stale_tmp(session_dir)
            mods = _load_from_disk(session_dir)
        previous = list(mods)
        ret = mutator(mods)
        if isinstance(ret, list):
            mods = ret
        _flush_to_disk(session_dir, mods)
        _publish_cache(session_dir, mods, _journal_identity(session_dir))
        try:
            undo_payloads.reclaim_removed(session_dir, previous, mods)
        except OSError:
            logger.warning('Undo payload reclamation deferred; committed history remains authoritative')
        return ret


def _with_recovered_session(function):
    @wraps(function)
    def recovered(base_path, *args, **kwargs):
        if not base_path:
            return function(base_path, *args, **kwargs)
        from lib.project_mod.write_recovery import recover_pending_writes
        session_dir = _get_session_dir(base_path)
        with locked_session(session_dir):
            recover_pending_writes(base_path)
            return function(base_path, *args, **kwargs)
    return recovered


@_with_recovered_session
def _start_new_session(base_path):
    """Warm the cache for ``base_path``'s session_dir and point the
    primary-view mirror at it.  Non-destructive: other session_dirs'
    caches are untouched, so background tasks still running against a
    previously-active project continue to record safely.
    """
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return None
    mods = _cache_get(session_dir)
    with _lock:
        _sync_primary_view(session_dir, mods)
    return session_dir


def _encode_original(original_content, mod):
    """Make ``original_content`` JSON-safe for the modifications record.

    Binary pre-images (captured by run_command when a command touches a
    non-UTF-8 file) arrive as ``bytes``, which ``json.dump`` cannot
    serialise — that would poison both ``modifications.json`` persistence
    and every ``get_state()``/jsonify response that mirrors this list.
    base64-encode bytes into a str and flag it so undo can decode back.
    """
    if isinstance(original_content, bytes):
        mod['originalContentB64'] = True
        return base64.b64encode(original_content).decode('ascii')
    return original_content


def _decode_original(mod):
    """Return a mod's original content as the right type for restore.

    Inverse of :func:`_encode_original`: base64-decodes back to ``bytes``
    when the record was flagged binary, else returns the stored str.
    """
    original = mod.get('originalContent')
    if mod.get('originalContentB64') and isinstance(original, str):
        return base64.b64decode(original)
    return original


def _with_journal_session(function):
    @wraps(function)
    def locked(base_path, *args, **kwargs):
        session_dir = _get_session_dir(base_path)
        if not session_dir:
            return False
        with locked_session(session_dir):
            return function(base_path, *args, **kwargs)
    return locked


def _same_recovery_evidence(existing, proposed):
    for key in ('basePath', 'path', 'convId', 'taskId', 'existed', 'postImage'):
        if existing.get(key) != proposed.get(key):
            return False
    def original(record):
        value = record.get('originalContent')
        if record.get('originalContentB64'):
            return base64.b64decode(value, validate=True)
        return value.encode('utf-8') if isinstance(value, str) else value
    return original(existing) == original(proposed)


class _RecoveryIdentityConflict(ValueError):
    """A change ID must never acknowledge different durable evidence."""


@_with_journal_session
def _record_modification(base_path, mod_type, path, original_content=None, reverse_patch=None, conv_id=None, task_id=None, action=None, fingerprint_byte_budget=None,
                         change_id=None, post_image=None):
    """Record a modification for later undo, tagged with conv_id and task_id.

    Args:
        mod_type: 'write_file', 'apply_diff', or 'run_command'.
        conv_id: Conversation ID for per-conversation rollback.
        task_id: Task ID for per-round rollback (one user message = one task).
        action: Optional live-display action hint in the settlement
            vocabulary (run_command already knows deleted/created/modified
            at record time; other types derive it from ``existed``).
    """
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return False

    if change_id is not None and (not isinstance(change_id, str)
                                  or not re.fullmatch(r'[0-9a-f]{32}', change_id)):
        raise ValueError('Invalid prepared change identity')
    mod = {
        'changeId': change_id or uuid.uuid4().hex,
        'type': mod_type,
        'path': path,
        'timestamp': time.time(),
    }
    # Record the absolute base_path so undo can recover WHICH project a
    #   round belongs to from a taskId/convId alone — without trusting the
    #   globally-active UI project (_state['path']), which may point at a
    #   different project when conversations are edited concurrently.
    try:
        mod['basePath'] = os.path.abspath(base_path) if base_path else ''
    except Exception as e:
        logger.debug('[Modifications] basePath abspath failed for %s: %s', base_path, e)
    # Freeze auxiliary-worktree capability before the checkout can be merged
    # or removed. A task's primary managed checkout keeps normal undo; a
    # manually added checkout sharing its Git history is display evidence,
    # not authority for the single-root undo gateway to undo canonical bytes.
    try:
        from lib.project_mod.config import get_conv_roots
        from lib.project_mod.write_tools._paths import _git_common_dir
        execution_roots = get_conv_roots(conv_id)
        primary = next(iter(execution_roots.values()), {}).get('path')
        abs_base = mod.get('basePath')
        if primary and abs_base:
            for name, state in execution_roots.items():
                if os.path.abspath(state['path']) == abs_base:
                    mod['root'] = name
                    break
            if os.path.abspath(primary) != abs_base:
                common = _git_common_dir(abs_base)
                if common and common == _git_common_dir(primary):
                    mod['undoAvailable'] = False
    except Exception as error:
        logger.debug('[Modifications] auxiliary root capability probe failed: %s',
                     type(error).__name__)
    from lib.project_mod.tool_admission import journal_conv_id
    conv_id = journal_conv_id(conv_id)
    if conv_id:
        mod['convId'] = conv_id
    if task_id:
        mod['taskId'] = task_id

    # Record workspace-root name so the frontend can display a
    #   'rootname:path' prefix for modifications made outside the primary
    #   root in multi-root workspaces.  base_path is the absolute root
    #   path the tool was actually executed against (result of
    #   _resolve_base / resolve_namespaced_path).
    try:
        abs_base = os.path.abspath(base_path) if base_path else ''
        with _lock:
            for rn, rs in _roots.items():
                if os.path.abspath(rs['path']) == abs_base:
                    mod.setdefault('root', rn)
                    break
    except Exception as e:
        logger.debug('[Modifications] root-name lookup failed for %s: %s', base_path, e)

    if mod_type == 'write_file':
        if original_content is not None:
            mod['originalContent'] = _encode_original(original_content, mod)
            mod['existed'] = True
        else:
            mod['existed'] = False
    elif mod_type == 'apply_diff':
        mod['reversePatch'] = reverse_patch  # {search, replace}
        if original_content is not None:
            # Pre-image is also stored as a backup blob (when small enough)
            # so undo can use the deterministic file-history restore path.
            mod['existed'] = True
            mod['originalContent'] = _encode_original(original_content, mod)
    elif mod_type == 'run_command':
        # Absence of backup bytes means capture was unavailable, not that the
        # file was absent. Only the snapshot's explicit creation observation
        # may authorize undo-by-delete. Unknown legacy callers fail closed.
        mod['action'] = action if action in ('created', 'deleted', 'modified') else 'unknown'
        mod['existed'] = action != 'created' or original_content is not None
        if original_content is not None:
            mod['originalContent'] = _encode_original(original_content, mod)

    # Capture what this change left on disk, separately from its pre-image.
    # Content evidence survives atomic inode replacement during multi-step
    # rollback, while detecting same-size edits on coarse timestamp filesystems.
    if post_image is not None:
        # Already verified by the prepared write; do not re-read/hash the file
        # only to discard that sample in favor of the durable intent evidence.
        mod['postImage'] = dict(post_image)
    else:
        try:
            from lib.project_mod.undo_filesystem import fingerprint_file
            target = _resolve_undo_target(base_path, mod)
            mod['postImage'] = fingerprint_file(
                base_path, target, byte_budget=fingerprint_byte_budget,
                file_byte_limit=MAX_FILE_SIZE if mod_type == 'run_command' else None)
        except (OSError, TypeError, ValueError):
            mod['postImage'] = {'kind': 'unavailable'}

    duplicate = False

    def _append(rows):
        nonlocal duplicate
        existing = (next((row for row in rows if row.get('changeId') == change_id), None)
                    if change_id is not None else None)
        if existing is not None:
            if not _same_recovery_evidence(undo_payloads.hydrate_record(session_dir, existing), mod):
                raise _RecoveryIdentityConflict('Prepared change identity has conflicting journal evidence')
            # The same locked RMW reload checks identity and confirms durability;
            # a separate full-journal read would double work for every edit.
            duplicate = True
            return
        rows.append(mod)

    try:
        _locked_rmw(session_dir, _append)

    except _RecoveryIdentityConflict:
        raise
    except Exception as e:
        logger.error('[Modifications] record failed type=%s path=%s session=%s: %s',
                     mod_type, path, os.path.basename(session_dir), e, exc_info=True)
        return False

    logger.debug('Recorded modification: %s %s (conv=%s task=%s session=%s)',
                 mod_type, path, conv_id or '?', task_id or '?',
                 os.path.basename(session_dir))

    if duplicate:
        return True

    # This durable side projection still uses a process-local history lock.
    # Preserve cross-process journal commit order until it has its own ordering
    # fence: process-local deferred lanes alone cannot prevent a late pre-image
    # from regressing the tracked version after another process snapshots it.
    # Exact undo evidence is already durable; no second journal rewrite is needed.
    try:
        if mod.get('existed', True) and original_content is not None:
            from lib import file_history as fh
            with timing_phase('file_history'):
                fh.track_edit(base_path, path, message_id=task_id,
                              pre_content=original_content, task_id=task_id)
    except Exception as e:
        logger.debug('[Modifications] file-history track_edit failed for %s: %s', path, e)

    listeners = tuple(_live_change_listeners) if task_id else ()
    if not listeners:
        return True

    def project_committed_change():
        entry = {
            'path': path, 'changeId': mod['changeId'], 'source': 'local',
            **({'rootPath': mod['basePath']} if mod.get('basePath') else {}),
            'action': action or _default_live_action(mod_type, mod.get('existed', True)),
            **({'root': mod['root']} if mod.get('root') else {}),

            **({'undoAvailable': False} if mod.get('undoAvailable') is False else {}),
        }
        for listener in listeners:
            try:
                listener(task_id, entry)
            except Exception as e:
                logger.debug('[Modifications] live-change listener failed for %s: %s', path, e)

    after_session_release(session_dir, project_committed_change, key=path)
    return True


@_with_recovered_session
def get_modifications(base_path, conv_id=None, *, include_content=True):
    """Get list of pending modifications for undo, optionally filtered by conv_id."""
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return []
    # Hold admission through selected hydration: concurrent undo cannot reclaim
    # a pack between the metadata snapshot and this reader's exact byte read.
    with locked_session(session_dir):
        mods = list(_cache_get(session_dir))
        if conv_id:
            mods = [m for m in mods if m.get('convId') == conv_id]
        if not include_content:
            return [dict(mod) for mod in mods]
        return [undo_payloads.hydrate_record(session_dir, mod) for mod in mods]


@_with_recovered_session
def get_task_modification_paths(base_path, task_id, *, conv_id=None, limit,
                                exclude=()):
    """Project at most ``limit`` distinct exact-task file addresses.

    Recovery and journal identity validation are the same as ordinary history
    reads. Borrow the validated cache under session admission, without copying
    historical rows or hydrating undo contents. A cold cache still loads the
    complete authoritative index; this is a bounded projection, not a new index.
    Returned dictionaries contain only immutable string values, never cache rows.
    """
    if type(limit) is not int or limit < 0:
        raise ValueError('Modification path limit must be a nonnegative integer')
    if not task_id or not limit:
        return []
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return []
    seen = set(exclude)
    result = []
    with locked_session(session_dir):
        for row in _cache_get(session_dir):
            if row.get('taskId') != task_id or (conv_id and row.get('convId') != conv_id):
                continue
            path = row.get('path')
            root = row.get('basePath') or base_path
            if not isinstance(path, str) or not path or str(root).startswith('remote:'):
                continue
            address = (path, str(root))
            if address in seen:
                continue
            seen.add(address)
            result.append({'path': path, 'rootPath': str(root)})
            if len(result) >= limit:
                break
    return result


@_with_recovered_session
def get_conv_ids_with_modifications(base_path):
    """Get list of conversation IDs that have pending modifications."""
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return []
    mods = list(_cache_get(session_dir))
    return list({m.get('convId') for m in mods if m.get('convId')})


def _resolve_undo_target(base_path, modification):
    """Validate stored path attribution before any undo reads or mutations.

    The journal is evidence of a change, not authority to access another root.
    Symlinks and special files are not interchangeable with tracked files.
    """
    base = os.path.realpath(os.path.abspath(base_path))
    recorded_base = modification.get('basePath')
    if recorded_base and os.path.realpath(os.path.abspath(recorded_base)) != base:
        raise ValueError('Modification belongs to a different project root')
    path = modification['path']
    if not isinstance(path, str) or not path or '\x00' in path:
        raise ValueError('Invalid modification path')
    target = os.path.abspath(os.path.join(base, path))
    if target == base or os.path.commonpath([base, target]) != base:
        raise ValueError('Undo path escapes the selected project root')
    relative = os.path.relpath(target, base)
    current = base
    parts = relative.split(os.sep)
    for index, component in enumerate(parts):
        current = os.path.join(current, component)
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            break
        reparse_point = getattr(metadata, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
        if stat.S_ISLNK(metadata.st_mode) or reparse_point:
            raise ValueError('Undo path contains a symlink or reparse point')
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError('Undo parent is not a directory')
        if index == len(parts) - 1 and not stat.S_ISREG(metadata.st_mode):
            raise ValueError('Undo target is not a regular file')
    return target


def _undo_modifications_list(base_path, modifications, *, completed_change_ids=None):
    """Internal: undo a list of modifications in reverse order. Returns (undone, failed)."""
    from lib.project_mod import undo_filesystem
    undone = []
    failed = []
    blocked_paths = set()
    from lib.project_mod.write_intents import recovery_file_bytes
    fingerprint_byte_budget = [max(UNDO_BATCH_CONTENT_BYTES, recovery_file_bytes() + 1)]

    def mark_undone(entry):
        undone.append(entry)
        change_id = mod.get('changeId')
        if completed_change_ids is not None and isinstance(change_id, str) and change_id:
            completed_change_ids.add(change_id)

    def mark_failed(entry):
        failed.append(entry)
        blocked_paths.add(os.path.abspath(target))

    # Optional file-history short-circuit: if the mod carries a backup
    # version we can restore deterministically from the backup blob.
    try:
        from lib import file_history as fh
        from lib.file_history.store import read_blob as _fh_read_blob
    except Exception as e:  # pragma: no cover — defensive
        logger.debug('[Modifications] file_history import failed: %s', e)
        fh = None
        _fh_read_blob = None
    for mod in reversed(modifications):
        mod_type = mod['type']
        path = mod['path']
        try:
            target = _resolve_undo_target(base_path, mod)
        except (OSError, TypeError, ValueError) as exc:
            failed.append({'path': path, 'reason': str(exc)})
            continue
        if os.path.abspath(target) in blocked_paths:
            mark_failed({'path': path, 'reason': 'A later change to this path could not be undone'})
            continue
        try:
            mod = undo_payloads.hydrate_record(_get_session_dir(base_path), mod)
        except (OSError, ValueError, TypeError):
            mark_failed({'path': path, 'reason': 'Undo payload is unavailable or corrupt'})
            continue
        post_image = mod.get('postImage')
        legacy_patch = post_image is None and mod_type == 'apply_diff' and 'reversePatch' in mod
        if not legacy_patch:
            if (not isinstance(post_image, dict) or post_image.get('kind') not in ('content', 'absent')
                    or not undo_filesystem.valid_postimage(post_image)):
                mark_failed({'path': path, 'reason': 'Undo has no verified post-image evidence'})
                continue
            current_image = undo_filesystem.fingerprint_file(
                base_path, target, byte_budget=fingerprint_byte_budget)
            if current_image != post_image:
                mark_failed({'path': path, 'reason': 'File changed after the recorded modification'})
                continue
        # Legacy reverse patches may match their exact replacement against a
        # bounded live read, but must never bypass that check via a full blob.
        # ── Preferred path: file-history blob restore (works for any mod_type) ──
        try:
            v = mod.get('fhVersion')
            if (v and not legacy_patch and mod.get('existed', True)
                    and fh and fh.is_enabled() and _fh_read_blob):
                blob = _fh_read_blob(base_path, path, int(v))
                if blob is not None:
                    undo_filesystem.write_file(base_path, target, blob, expected_image=post_image)
                    _nudge_vscode(base_path, target)
                    mark_undone({'type': 'fh_restore', 'path': path})
                    logger.info('Undo: restored %s via fh@v%s', path, v)
                    continue
        except Exception as e:
            logger.debug('[Modifications] fh undo fallback for %s: %s', path, e)
        try:
            if mod_type == 'write_file':
                if not mod.get('existed', True):
                    if os.path.exists(target):
                        undo_filesystem.remove_file(base_path, target, expected_image=post_image)
                        mark_undone({'type': 'delete', 'path': path})
                        logger.info('Undo: deleted created file %s', path)
                else:
                    if 'originalContent' in mod and os.path.exists(target):
                        original = _decode_original(mod)
                        undo_filesystem.write_file(base_path, target, original, expected_image=post_image)
                        _nudge_vscode(base_path, target)
                        mark_undone({'type': 'restore', 'path': path})
                        logger.info('Undo: restored original content for %s', path)
                    else:
                        mark_failed({'path': path, 'reason': 'No original content available'})
            elif mod_type == 'apply_diff':
                if 'reversePatch' in mod and os.path.exists(target):
                    content = undo_filesystem.read_text(base_path, target)
                    rev = mod['reversePatch']
                    # rev['search'] = the text currently in file (the replacement)
                    # rev['replace'] = the original text we want to restore
                    if rev['search'] not in content:
                        mark_failed({
                            'path': path,
                            'reason': 'Reverse patch target no longer matches',
                        })
                        continue
                    new_content = content.replace(rev['search'], rev['replace'], 1)
                    undo_filesystem.write_file(base_path, target, new_content,
                                               expected_content=content.encode('utf-8'), expected_image=post_image)
                    _nudge_vscode(base_path, target)
                    mark_undone({'type': 'reverse_patch', 'path': path})
                    logger.info('Undo: reversed patch for %s', path)
                else:
                    mark_failed({'path': path, 'reason': 'No reverse patch available'})
            elif mod_type == 'run_command':
                if not mod.get('existed', True) and mod.get('action') != 'created':
                    mark_failed({'path': path, 'reason': 'Legacy command record does not prove file creation'})
                    continue
                if not mod.get('existed', True):
                    # File was CREATED by the command → delete it
                    if os.path.exists(target):
                        undo_filesystem.remove_file(base_path, target, expected_image=post_image)
                        mark_undone({'type': 'delete', 'path': path})
                        logger.info('Undo run_command: deleted created file %s', path)
                    else:
                        mark_undone({'type': 'already_gone', 'path': path})
                else:
                    # File was DELETED or MODIFIED → restore original content
                    if 'originalContent' in mod:
                        original = _decode_original(mod)
                        undo_filesystem.write_file(base_path, target, original, expected_image=post_image)
                        _nudge_vscode(base_path, target)
                        mark_undone({'type': 'restore', 'path': path})
                        logger.info('Undo run_command: restored %s', path)
                    else:
                        mark_failed({'path': path, 'reason': 'No original content saved for run_command change'})
        except Exception as e:
            mark_failed({'path': path, 'reason': str(e)})
            logger.error('Undo failed for %s: %s', path, e, exc_info=True)
    return undone, failed


@_with_recovered_session
def undo_conv_modifications(base_path, conv_id):
    """Undo modifications for a specific conversation (对话粒度回撤)."""
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return {'ok': False, 'error': 'No session found'}

    conv_mods = [m for m in _cache_get(session_dir) if m.get('convId') == conv_id]

    if not conv_mods:
        return {'ok': True, 'message': 'No modifications to undo for this conversation',
                'undone': 0, 'failed': 0}

    completed_change_ids = set()
    undone, failed = _undo_modifications_list(
        base_path, conv_mods, completed_change_ids=completed_change_ids)

    def _filter(mods):
        return [m for m in mods if m.get('changeId') not in completed_change_ids]

    _locked_rmw(session_dir, _filter)

    return {
        'ok': True,
        'undone': len(undone),
        'failed': len(failed),
        'convId': conv_id,
        'details': {'undone': undone, 'failed': failed},
    }


@_with_recovered_session
def undo_task_modifications(base_path, task_id):
    """Undo modifications for a specific task/round (本轮对话回撤).

    Each user message creates one task. This undoes only the file changes
    made during that single round of conversation.
    """
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return {'ok': False, 'error': 'No session found'}

    task_mods = [m for m in _cache_get(session_dir) if m.get('taskId') == task_id]

    if not task_mods:
        return {'ok': True, 'message': 'No modifications to undo for this task',
                'undone': 0, 'failed': 0}

    completed_change_ids = set()
    undone, failed = _undo_modifications_list(
        base_path, task_mods, completed_change_ids=completed_change_ids)

    def _filter(mods):
        return [m for m in mods if m.get('changeId') not in completed_change_ids]

    _locked_rmw(session_dir, _filter)

    return {
        'ok': True,
        'undone': len(undone),
        'failed': len(failed),
        'taskId': task_id,
        'details': {'undone': undone, 'failed': failed},
    }


def redo_task_modifications(base_path, task_id):
    """Re-apply a previously-undone round via the file-history snapshot.

    Unlike undo_*, this does NOT replay individual modification records —
    instead it locates the snapshot tagged with ``task_id`` and calls
    :func:`lib.file_history.restore_from` to re-apply the full set of
    file versions with per-file anchored mutations. Returns the same shape as
    :func:`undo_task_modifications` so the frontend can reuse display code.
    """
    try:
        from lib import file_history as fh
    except Exception as e:
        logger.warning('[Modifications] redo requires file_history: %s', e)
        return {'ok': False, 'error': f'file_history unavailable: {e}'}
    if not fh.is_enabled():
        return {'ok': False, 'error': 'File-history disabled (TOFU_FILE_HISTORY=0)'}
    if not task_id:
        return {'ok': False, 'error': 'taskId is required'}
    snap_id = fh.find_latest_snapshot_id(base_path, task_id=task_id)
    if not snap_id:
        return {'ok': False, 'taskId': task_id,
                'error': 'No snapshot found for this task (was it ever committed?)'}
    result = fh.restore_from(base_path, snap_id)
    if not result.get('ok'):
        files = [f['path'] for f in result.get('files', [])]
        return {'ok': False, 'taskId': task_id,
                'error': result.get('error') or 'restore failed',
                'redone': len(files), 'files': files,
                'details': {'failed': result.get('failed', [])}}
    files = [f.get('path') for f in result.get('files', []) if f.get('path')]
    logger.info('[Modifications] redo task=%s files=%d snap=%s',
                task_id, len(files), snap_id[:8])
    return {
        'ok': True,
        'taskId': task_id,
        'redone': len(files),
        'files': files,
        'sha': result.get('newSnapshotId'),
        'snapshotId': result.get('newSnapshotId'),
    }


@_with_recovered_session
def undo_all_modifications(base_path):
    """Undo all recorded modifications in reverse order."""
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return {'ok': False, 'error': 'No session found'}

    modifications = list(_cache_get(session_dir))

    if not modifications:
        return {'ok': True, 'message': 'No modifications to undo', 'undone': 0, 'failed': 0}

    completed_change_ids = set()
    undone, failed = _undo_modifications_list(
        base_path, modifications, completed_change_ids=completed_change_ids)

    def _clear(mods):
        return [m for m in mods if m.get('changeId') not in completed_change_ids]

    _locked_rmw(session_dir, _clear)

    return {
        'ok': True,
        'undone': len(undone),
        'failed': len(failed),
        'details': {'undone': undone, 'failed': failed},
    }
