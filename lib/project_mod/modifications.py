"""Modification history, undo/redo, and session tracking.

★ 2026-04-22 — Per-session-dir storage (multi-project concurrency safe)
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
import hashlib
import json
import os
import tempfile
import time

from lib.log import get_logger
from lib.project_mod.config import (
    SESSIONS_DIR,
    _lock,
    _roots,
    _state,
)

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  Per-session-dir in-memory cache
# ═══════════════════════════════════════════════════════════════════
# Maps absolute session_dir → list[mod dict].  Loaded on demand from
# the session's modifications.json.  All access is guarded by _lock.
_mods_cache: dict[str, list] = {}

# Tracks session_dirs we have already loaded from disk in this process
# (even if the resulting list was empty).  Prevents redundant disk reads
# while still allowing a cold-path load for fresh session_dirs.
_loaded_dirs: set[str] = set()


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
        except OSError:
            pass
        raise


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
    return session_dir


# Session dirs whose stale-tmp sweep has already run in this process.
# We only clean at cold-load time so an in-flight _atomic_json_write
# from a concurrent thread cannot have its .tmp rug-pulled.
_stale_cleaned: set[str] = set()

# Only remove .tmp files older than this — crash artifacts are much
# older; in-flight atomic writes are sub-second.
_STALE_TMP_AGE_SECONDS = 60.0


def _clean_stale_tmp(session_dir):
    """Remove leftover atomic-write temp files from previous crashes.

    Safety rails:
      - Only runs **once per session_dir per process** (see ``_stale_cleaned``).
      - Only deletes files older than :data:`_STALE_TMP_AGE_SECONDS` so a
        concurrent in-flight :func:`_atomic_json_write` on the same
        session_dir cannot have its fresh .tmp file deleted before
        ``os.replace`` runs.

    Caller must hold ``_lock`` (we touch the shared ``_stale_cleaned`` set).
    """
    if session_dir in _stale_cleaned:
        return
    _stale_cleaned.add(session_dir)
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
    if session_dir in _loaded_dirs:
        return _mods_cache.setdefault(session_dir, [])
    mods = _load_from_disk(session_dir)
    _mods_cache[session_dir] = mods
    _loaded_dirs.add(session_dir)
    if mods:
        logger.info('[Modifications] loaded %d pending records from %s',
                    len(mods), os.path.basename(session_dir))
    return mods


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
        _clean_stale_tmp(session_dir)    # safe: one-shot + age-gated
        _cache_get(session_dir)          # force load if cold
        _sync_primary_view(session_dir)
    return session_dir


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
    if conv_id:
        mod['convId'] = conv_id
    if task_id:
        mod['taskId'] = task_id

    # ★ Record workspace-root name so the frontend can display a
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
            mod['originalContent'] = original_content
            mod['existed'] = True
        else:
            mod['existed'] = False
    elif mod_type == 'apply_diff':
        mod['reversePatch'] = reverse_patch  # {search, replace}
    elif mod_type == 'run_command':
        # run_command changes: original_content=None means file was created
        # (didn't exist), original_content=<str|bytes> means file was deleted
        # or modified (save for restore).
        if original_content is not None:
            mod['originalContent'] = original_content
            mod['existed'] = True
        else:
            mod['existed'] = False

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
    for mod in reversed(modifications):
        mod_type = mod['type']
        path = mod['path']
        target = os.path.join(base_path, path) if not os.path.isabs(path) else path
        try:
            if mod_type == 'write_file':
                if not mod.get('existed', True):
                    if os.path.exists(target):
                        os.remove(target)
                        undone.append({'type': 'delete', 'path': path})
                        logger.info('Undo: deleted created file %s', path)
                else:
                    if 'originalContent' in mod and os.path.exists(target):
                        with open(target, 'w', newline='') as f:
                            f.write(mod['originalContent'])
                            f.flush()
                            os.fsync(f.fileno())
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
                    new_content = content.replace(rev['search'], rev['replace'], 1)
                    with open(target, 'w', newline='') as f:
                        f.write(new_content)
                        f.flush()
                        os.fsync(f.fileno())
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
                        original = mod['originalContent']
                        parent_dir = os.path.dirname(target)
                        if parent_dir and not os.path.isdir(parent_dir):
                            os.makedirs(parent_dir, exist_ok=True)
                        if isinstance(original, bytes):
                            with open(target, 'wb') as f:
                                f.write(original)
                                f.flush()
                                os.fsync(f.fileno())
                        else:
                            with open(target, 'w', newline='') as f:
                                f.write(original)
                                f.flush()
                                os.fsync(f.fileno())
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
