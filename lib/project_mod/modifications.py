"""Modification history, undo/redo, and session tracking."""
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


def _start_new_session(base_path):
    """Start a new modification session."""
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return None
    session_file = os.path.join(session_dir, 'modifications.json')
    # Clean up stale .tmp files from previously crashed atomic writes
    try:
        for name in os.listdir(session_dir):
            if name.startswith('.modifications_') and name.endswith('.tmp'):
                stale = os.path.join(session_dir, name)
                os.unlink(stale)
                logger.debug('Cleaned up stale temp file: %s', name)
    except OSError as e:
        logger.debug('Failed to clean stale temp files: %s', e)
    with _lock:
        _state['sessionId'] = session_dir
        _state['modifications'] = []
    # Load existing modifications if any
    if os.path.exists(session_file):
        try:
            with open(session_file) as f:
                data = json.load(f)
            with _lock:
                _state['modifications'] = data.get('modifications', [])
            logger.info('Loaded %d pending modifications', len(_state["modifications"]))
        except Exception as e:
            logger.error('Failed to load modifications (corrupt file?): %s', e, exc_info=True)
            # Rename corrupt file so we don't fail on every restart
            corrupt_path = session_file + '.corrupt'
            try:
                os.replace(session_file, corrupt_path)
                logger.warning('Renamed corrupt modifications file to %s', corrupt_path)
            except OSError as rename_err:
                logger.warning('Could not rename corrupt file: %s', rename_err)
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
        'type': mod_type,  # 'write_file', 'apply_diff', or 'run_command'
        'path': path,
        'timestamp': time.time(),
    }
    if conv_id:
        mod['convId'] = conv_id
    if task_id:
        mod['taskId'] = task_id
    # ★ Record workspace-root name so the frontend can display a
    #   'rootname:path' prefix for modifications made outside the primary
    #   root in multi-root workspaces. base_path is the absolute root
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
            # Store original content for new files, or None if file didn't exist
            mod['originalContent'] = original_content
            mod['existed'] = True
        else:
            mod['existed'] = False
    elif mod_type == 'apply_diff':
        mod['reversePatch'] = reverse_patch  # {search, replace}
    elif mod_type == 'run_command':
        # run_command changes: original_content=None means file was created (didn't exist),
        # original_content=<str|bytes> means file was deleted or modified (save for restore)
        if original_content is not None:
            mod['originalContent'] = original_content
            mod['existed'] = True
        else:
            mod['existed'] = False

    with _lock:
        _state['modifications'].append(mod)
        # Save to disk (atomic write to prevent corruption on crash)
        session_file = os.path.join(session_dir, 'modifications.json')
        try:
            _atomic_json_write(session_file, {'modifications': _state['modifications']})
        except Exception as e:
            logger.error('Failed to save modifications: %s', e, exc_info=True)

    logger.debug('Recorded modification: %s %s (conv=%s task=%s)', mod_type, path, conv_id or '?', task_id or '?')
    return True


def get_modifications(base_path, conv_id=None):
    """Get list of pending modifications for undo, optionally filtered by conv_id."""
    with _lock:
        mods = list(_state['modifications'])
    if conv_id:
        mods = [m for m in mods if m.get('convId') == conv_id]
    return mods


def get_conv_ids_with_modifications(base_path):
    """Get set of conversation IDs that have pending modifications."""
    with _lock:
        return list(set(m.get('convId') for m in _state['modifications'] if m.get('convId')))


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
                # run_command changes: undo by reversing the filesystem change
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


def _save_modifications(session_dir):
    """Save current modifications to disk (atomic write)."""
    session_file = os.path.join(session_dir, 'modifications.json')
    try:
        with _lock:
            mods = list(_state['modifications'])
        if mods:
            _atomic_json_write(session_file, {'modifications': mods})
        else:
            if os.path.exists(session_file):
                os.remove(session_file)
    except Exception as e:
        logger.error('Failed to save modifications: %s', e, exc_info=True)


def undo_conv_modifications(base_path, conv_id):
    """Undo modifications for a specific conversation (对话粒度回撤)."""
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return {'ok': False, 'error': 'No session found'}

    with _lock:
        all_mods = list(_state['modifications'])

    conv_mods = [m for m in all_mods if m.get('convId') == conv_id]
    if not conv_mods:
        return {'ok': True, 'message': 'No modifications to undo for this conversation', 'undone': 0, 'failed': 0}

    undone, failed = _undo_modifications_list(base_path, conv_mods)

    # Remove undone mods from state, keep others
    with _lock:
        _state['modifications'] = [m for m in _state['modifications'] if m.get('convId') != conv_id]
    _save_modifications(session_dir)

    return {
        'ok': True,
        'undone': len(undone),
        'failed': len(failed),
        'convId': conv_id,
        'details': {'undone': undone, 'failed': failed}
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
        all_mods = list(_state['modifications'])

    task_mods = [m for m in all_mods if m.get('taskId') == task_id]
    if not task_mods:
        return {'ok': True, 'message': 'No modifications to undo for this task', 'undone': 0, 'failed': 0}

    undone, failed = _undo_modifications_list(base_path, task_mods)

    # Remove undone mods from state, keep others
    with _lock:
        _state['modifications'] = [m for m in _state['modifications'] if m.get('taskId') != task_id]
    _save_modifications(session_dir)

    return {
        'ok': True,
        'undone': len(undone),
        'failed': len(failed),
        'taskId': task_id,
        'details': {'undone': undone, 'failed': failed}
    }


def undo_all_modifications(base_path):
    """Undo all recorded modifications in reverse order."""
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return {'ok': False, 'error': 'No session found'}

    with _lock:
        modifications = list(_state['modifications'])

    if not modifications:
        return {'ok': True, 'message': 'No modifications to undo', 'undone': 0, 'failed': 0}

    undone, failed = _undo_modifications_list(base_path, modifications)

    # Clear all modifications
    with _lock:
        _state['modifications'] = []
    _save_modifications(session_dir)

    return {
        'ok': True,
        'undone': len(undone),
        'failed': len(failed),
        'details': {'undone': undone, 'failed': failed}
    }

