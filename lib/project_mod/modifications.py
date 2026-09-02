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
import json
import os
import tempfile
import time

from lib.json_store import write_bytes_atomic, write_text_atomic
from lib.log import get_logger
from lib.project_mod.config import (
    SESSIONS_DIR,
    _lock,
    _roots,
    _state,
)
from runtime_guards import resolve_resource_budget

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  Per-session-dir in-memory cache
# ═══════════════════════════════════════════════════════════════════
# Maps absolute session_dir → list[mod dict]. Disk remains authoritative, so an
# LRU eviction only trades a later read for bounded resident memory.
_MODS_CACHE_CAPACITY = resolve_resource_budget(
    'TOFU_PROJECT_UNDO_CACHE_CAPACITY', maximum=2048)
_mods_cache: OrderedDict[str, list] = OrderedDict()


def _atomic_json_write(filepath, data):
    """Write JSON data to file atomically (write to temp, then rename).

    Prevents file corruption if the process is killed mid-write.
    os.replace() is atomic on POSIX when src and dst are on the same filesystem.
    """
    dir_name = os.path.dirname(filepath)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp', prefix='.modifications_')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
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


def _nudge_vscode(filepath):
    """Bump mtime so VS Code's file watcher detects the restored file."""
    try:
        st = os.stat(filepath)
        os.utime(filepath, (st.st_atime, st.st_mtime + 0.000001))
    except OSError as e:
        logger.debug('Failed to bump mtime for VS Code watcher on %s: %s', filepath, e)


def _get_session_dir(base_path):
    """Get session directory for storing modification history."""
    if not base_path:
        return None
    session_id = hashlib.md5(base_path.encode()).hexdigest()[:12]
    session_dir = os.path.join(SESSIONS_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)
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
                marker_root = str(json.load(f).get('basePath') or '')
            if marker_root:
                history_roots.append(marker_root)
        except (OSError, TypeError, ValueError):
            pass
        session_file = os.path.join(session_dir, 'modifications.json')
        if not os.path.isfile(session_file):
            continue
        try:
            with open(session_file) as f:
                mods = json.load(f).get('modifications', [])
        except Exception as e:
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

            for base_path in history_roots:
                for entry in file_history.list_history(base_path, limit=2000):
                    if task_id and entry.get('taskId') == task_id:
                        return base_path
                    if conv_id and not task_id and entry.get('convId') == conv_id:
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

    Caller must hold ``_lock`` so it cannot race this module's writer.
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
    """Load modifications list from disk.  Returns ``[]`` on missing/corrupt.

    Side-effect: renames a corrupt file to ``<name>.corrupt`` so subsequent
    loads don't keep failing.  Caller must hold ``_lock``.
    """
    session_file = os.path.join(session_dir, 'modifications.json')
    if not os.path.exists(session_file):
        return []
    try:
        with open(session_file) as f:
            data = json.load(f)
        mods = data.get('modifications', [])
        if not isinstance(mods, list):
            logger.warning('[Modifications] %s: modifications is not a list, ignoring', session_file)
            return []
        return mods
    except Exception as e:
        logger.error('[Modifications] failed to load %s (corrupt file?): %s',
                     session_file, e, exc_info=True)
        corrupt_path = session_file + '.corrupt'
        try:
            os.replace(session_file, corrupt_path)
            logger.warning('[Modifications] renamed corrupt file to %s', corrupt_path)
        except OSError as rename_err:
            logger.warning('[Modifications] could not rename corrupt file: %s', rename_err)
        return []


def _cache_get(session_dir):
    """Return the cached mods list for ``session_dir``, loading on cold miss.

    Caller must hold ``_lock``.  Returns a *live* reference to the cached
    list — callers that mutate it must also call ``_flush_to_disk``.
    """
    cached = _mods_cache.get(session_dir)
    if cached is not None:
        _mods_cache.move_to_end(session_dir)
        return cached
    _clean_stale_tmp(session_dir)
    mods = _load_from_disk(session_dir)
    _mods_cache[session_dir] = mods
    while len(_mods_cache) > _MODS_CACHE_CAPACITY:
        evicted_dir, evicted_mods = _mods_cache.popitem(last=False)
        logger.debug(
            '[Modifications] evicted undo cache session=%s records=%d '
            '(capacity=%d; disk remains authoritative)',
            os.path.basename(evicted_dir), len(evicted_mods),
            _MODS_CACHE_CAPACITY,
        )
    if mods:
        logger.info('[Modifications] loaded %d pending records from %s',
                    len(mods), os.path.basename(session_dir))
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

    Removes the file when the list is empty.  Caller must hold ``_lock``.
    """
    session_file = os.path.join(session_dir, 'modifications.json')
    try:
        if mods:
            _atomic_json_write(session_file, {'modifications': mods})
        else:
            if os.path.exists(session_file):
                os.remove(session_file)
    except Exception as e:
        logger.error('[Modifications] failed to save %s: %s',
                     session_file, e, exc_info=True)


def _sync_primary_view(session_dir):
    """Mirror the cached mods for ``session_dir`` into ``_state`` so that
    ``get_state()`` / the UI badge reflects the currently-active primary
    root.  ``_state['modifications']`` is intentionally **read-only** from
    the rest of the codebase — nothing should ever mutate it directly.
    Caller must hold ``_lock``.
    """
    mods = _mods_cache.get(session_dir, [])
    # Store a shallow copy so UI iteration can't tear on subsequent
    # background mutations.
    _state['sessionId'] = session_dir
    _state['modifications'] = list(mods)


def _locked_rmw(session_dir, mutator):
    """Atomic read-modify-write on ``session_dir``'s mod list.

    ``mutator(mods)`` receives the cached list and may mutate it in-place
    (or return a replacement list, which will overwrite the cache entry).
    The lock is held for the entire cache-read → mutate → disk-flush
    window so concurrent callers on the same session_dir see a consistent
    view.  Different session_dirs do not contend on each other beyond the
    short critical section that touches the shared ``_mods_cache`` dict.

    Returns whatever ``mutator`` returns (useful for getters).
    """
    with _lock:
        mods = _cache_get(session_dir)
        ret = mutator(mods)
        # Mutator may return a new list (replacement semantics) or None
        # (in-place mutation).
        if ret is not None and isinstance(ret, list) and ret is not mods:
            _mods_cache[session_dir] = ret
            _mods_cache.move_to_end(session_dir)
            mods = ret
        _flush_to_disk(session_dir, mods)
        # Keep the primary-root view fresh if this session_dir happens to
        # match the currently-active primary.  This is a pure mirror —
        # it does not drive persistence.
        if _state.get('sessionId') == session_dir:
            _state['modifications'] = list(mods)
        return ret


def _start_new_session(base_path):
    """Warm the cache for ``base_path``'s session_dir and point the
    primary-view mirror at it.  Non-destructive: other session_dirs'
    caches are untouched, so background tasks still running against a
    previously-active project continue to record safely.
    """
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return None
    with _lock:
        _cache_get(session_dir)          # force load if cold
        _sync_primary_view(session_dir)
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


def _record_modification(base_path, mod_type, path, original_content=None, reverse_patch=None, conv_id=None, task_id=None):
    """Record a modification for later undo, tagged with conv_id and task_id.

    Args:
        mod_type: 'write_file', 'apply_diff', or 'run_command'.
        conv_id: Conversation ID for per-conversation rollback.
        task_id: Task ID for per-round rollback (one user message = one task).
    """
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return False

    mod = {
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
                    mod['root'] = rn
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
    elif mod_type == 'run_command':
        # run_command changes: original_content=None means file was created
        # (didn't exist), original_content=<str|bytes> means file was deleted
        # or modified (save for restore).
        if original_content is not None:
            mod['originalContent'] = _encode_original(original_content, mod)
            mod['existed'] = True
        else:
            mod['existed'] = False

    # ── File-history: capture the PRE-write contents as a backup version
    #    so undo can restore byte-for-byte.  Note we run AFTER the write
    #    tool has already overwritten the file on disk, so we MUST pass
    #    ``original_content`` explicitly — reading from disk would just
    #    capture the post-image.  Silent no-op when the store is disabled
    #    or when we have no pre-image (mod['existed'] = False).
    try:
        if mod.get('existed', True) and original_content is not None:
            from lib import file_history as fh
            v = fh.track_edit(base_path, path, message_id=task_id,
                              pre_content=original_content,
                              task_id=task_id)
            if v is not None:
                mod['fhVersion'] = v
    except Exception as e:
        logger.debug('[Modifications] file-history track_edit failed for %s: %s', path, e)

    def _append(mods):
        mods.append(mod)

    try:
        _locked_rmw(session_dir, _append)
    except Exception as e:
        logger.error('[Modifications] record failed type=%s path=%s session=%s: %s',
                     mod_type, path, os.path.basename(session_dir), e, exc_info=True)
        return False

    logger.debug('Recorded modification: %s %s (conv=%s task=%s session=%s)',
                 mod_type, path, conv_id or '?', task_id or '?',
                 os.path.basename(session_dir))
    return True


def get_modifications(base_path, conv_id=None):
    """Get list of pending modifications for undo, optionally filtered by conv_id."""
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return []
    with _lock:
        mods = list(_cache_get(session_dir))
    if conv_id:
        mods = [m for m in mods if m.get('convId') == conv_id]
    return mods


def get_conv_ids_with_modifications(base_path):
    """Get list of conversation IDs that have pending modifications."""
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return []
    with _lock:
        mods = list(_cache_get(session_dir))
    return list({m.get('convId') for m in mods if m.get('convId')})


def _undo_modifications_list(base_path, modifications):
    """Internal: undo a list of modifications in reverse order. Returns (undone, failed)."""
    undone = []
    failed = []
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
        target = os.path.join(base_path, path) if not os.path.isabs(path) else path
        # ── Preferred path: file-history blob restore (works for any mod_type) ──
        try:
            v = mod.get('fhVersion')
            if (v and mod.get('existed', True)
                    and fh and fh.is_enabled() and _fh_read_blob):
                blob = _fh_read_blob(base_path, path, int(v))
                if blob is not None:
                    write_bytes_atomic(target, blob)
                    _nudge_vscode(target)
                    undone.append({'type': 'fh_restore', 'path': path})
                    logger.info('Undo: restored %s via fh@v%s', path, v)
                    continue
        except Exception as e:
            logger.debug('[Modifications] fh undo fallback for %s: %s', path, e)
        try:
            if mod_type == 'write_file':
                if not mod.get('existed', True):
                    if os.path.exists(target):
                        os.remove(target)
                        undone.append({'type': 'delete', 'path': path})
                        logger.info('Undo: deleted created file %s', path)
                else:
                    if 'originalContent' in mod and os.path.exists(target):
                        original = _decode_original(mod)
                        if isinstance(original, bytes):
                            write_bytes_atomic(target, original)
                        else:
                            write_text_atomic(target, original)
                        _nudge_vscode(target)
                        undone.append({'type': 'restore', 'path': path})
                        logger.info('Undo: restored original content for %s', path)
                    else:
                        failed.append({'path': path, 'reason': 'No original content available'})
            elif mod_type == 'apply_diff':
                if 'reversePatch' in mod and os.path.exists(target):
                    with open(target, errors='replace') as f:
                        content = f.read()
                    rev = mod['reversePatch']
                    # rev['search'] = the text currently in file (the replacement)
                    # rev['replace'] = the original text we want to restore
                    if rev['search'] not in content:
                        failed.append({
                            'path': path,
                            'reason': 'Reverse patch target no longer matches',
                        })
                        continue
                    new_content = content.replace(rev['search'], rev['replace'], 1)
                    write_text_atomic(target, new_content)
                    _nudge_vscode(target)
                    undone.append({'type': 'reverse_patch', 'path': path})
                    logger.info('Undo: reversed patch for %s', path)
                else:
                    failed.append({'path': path, 'reason': 'No reverse patch available'})
            elif mod_type == 'run_command':
                if not mod.get('existed', True):
                    # File was CREATED by the command → delete it
                    if os.path.exists(target):
                        os.remove(target)
                        undone.append({'type': 'delete', 'path': path})
                        logger.info('Undo run_command: deleted created file %s', path)
                    else:
                        undone.append({'type': 'already_gone', 'path': path})
                else:
                    # File was DELETED or MODIFIED → restore original content
                    if 'originalContent' in mod:
                        original = _decode_original(mod)
                        parent_dir = os.path.dirname(target)
                        if parent_dir and not os.path.isdir(parent_dir):
                            os.makedirs(parent_dir, exist_ok=True)
                        if isinstance(original, bytes):
                            write_bytes_atomic(target, original)
                        else:
                            write_text_atomic(target, original)
                        _nudge_vscode(target)
                        undone.append({'type': 'restore', 'path': path})
                        logger.info('Undo run_command: restored %s', path)
                    else:
                        failed.append({'path': path, 'reason': 'No original content saved for run_command change'})
        except Exception as e:
            failed.append({'path': path, 'reason': str(e)})
            logger.error('Undo failed for %s: %s', path, e, exc_info=True)
    return undone, failed


# Kept for backward compatibility — external callers (if any) still import it.
# Internally, mutations should go through _locked_rmw.
def _save_modifications(session_dir):
    """Flush the in-memory cache for ``session_dir`` to disk."""
    with _lock:
        mods = list(_cache_get(session_dir))
        _flush_to_disk(session_dir, mods)


def undo_conv_modifications(base_path, conv_id):
    """Undo modifications for a specific conversation (对话粒度回撤)."""
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return {'ok': False, 'error': 'No session found'}

    with _lock:
        conv_mods = [m for m in _cache_get(session_dir) if m.get('convId') == conv_id]

    if not conv_mods:
        return {'ok': True, 'message': 'No modifications to undo for this conversation',
                'undone': 0, 'failed': 0}

    undone, failed = _undo_modifications_list(base_path, conv_mods)

    def _filter(mods):
        return [m for m in mods if m.get('convId') != conv_id]

    _locked_rmw(session_dir, _filter)

    return {
        'ok': True,
        'undone': len(undone),
        'failed': len(failed),
        'convId': conv_id,
        'details': {'undone': undone, 'failed': failed},
    }


def undo_task_modifications(base_path, task_id):
    """Undo modifications for a specific task/round (本轮对话回撤).

    Each user message creates one task. This undoes only the file changes
    made during that single round of conversation.
    """
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return {'ok': False, 'error': 'No session found'}

    with _lock:
        task_mods = [m for m in _cache_get(session_dir) if m.get('taskId') == task_id]

    if not task_mods:
        return {'ok': True, 'message': 'No modifications to undo for this task',
                'undone': 0, 'failed': 0}

    undone, failed = _undo_modifications_list(base_path, task_mods)

    def _filter(mods):
        return [m for m in mods if m.get('taskId') != task_id]

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
    file versions atomically.  Returns the same shape as
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
    snap_id = None
    for entry in fh.list_history(base_path, limit=200):
        if entry.get('taskId') == task_id:
            snap_id = entry.get('id')
            break
    if not snap_id:
        return {'ok': False, 'taskId': task_id,
                'error': 'No snapshot found for this task (was it ever committed?)'}
    result = fh.restore_from(base_path, snap_id)
    if not result.get('ok'):
        return {'ok': False, 'taskId': task_id,
                'error': result.get('error') or 'restore failed'}
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


def undo_all_modifications(base_path):
    """Undo all recorded modifications in reverse order."""
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return {'ok': False, 'error': 'No session found'}

    with _lock:
        modifications = list(_cache_get(session_dir))

    if not modifications:
        return {'ok': True, 'message': 'No modifications to undo', 'undone': 0, 'failed': 0}

    undone, failed = _undo_modifications_list(base_path, modifications)

    def _clear(mods):
        return []

    _locked_rmw(session_dir, _clear)

    return {
        'ok': True,
        'undone': len(undone),
        'failed': len(failed),
        'details': {'undone': undone, 'failed': failed},
    }
