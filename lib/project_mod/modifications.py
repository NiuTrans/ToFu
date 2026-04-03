"""Modification history, undo/redo, and session tracking."""
import hashlib
import json
import os
import threading
import time

from lib.log import get_logger
from lib.project_mod.config import (
    SESSIONS_DIR,
    _lock,
    _state,
)

logger = get_logger(__name__)


def _nudge_vscode(filepath):
    """Bump mtime so VS Code's file watcher detects the restored file."""
    try:
        st = os.stat(filepath)
        os.utime(filepath, (st.st_atime, st.st_mtime + 0.000001))
    except OSError as e:
        logger.debug('Failed to bump mtime for VS Code watcher on %s: %s', filepath, e)

# Debounced auto-index on file changes — per-root tracking
_dirty_by_root = {}   # {abs_base_path: set of rel_paths}
_dirty_timer = None

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
            logger.error('Failed to load modifications: %s', e, exc_info=True)
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
        # Save to disk
        session_file = os.path.join(session_dir, 'modifications.json')
        try:
            with open(session_file, 'w') as f:
                json.dump({'modifications': _state['modifications']}, f, indent=2)
        except Exception as e:
            logger.error('Failed to save modifications: %s', e, exc_info=True)

    logger.debug('Recorded modification: %s %s (conv=%s task=%s)', mod_type, path, conv_id or '?', task_id or '?')
    # ★ Schedule smart index update for the changed file
    _schedule_index_update(base_path, path)
    return True


# ═══════════════════════════════════════════════════════
#  ★ Smart Auto-Update Index (debounced, background, per-root isolated)
# ═══════════════════════════════════════════════════════

def _schedule_index_update(base_path, rel_path):
    """Schedule a debounced re-index of modified files (3s delay to batch changes).

    Each root's dirty files are tracked independently so that switching
    the primary project or removing an extra root never mixes file
    descriptions from different projects.
    """
    global _dirty_timer
    with _lock:
        # Check whether this root has an active index to update.
        # Could be primary (_state) or an extra root (_roots).
        from lib.project_mod.config import _roots
        has_index = False
        if _state.get('path') == base_path and _state.get('index'):
            has_index = True
        else:
            for _rn, rs in _roots.items():
                if rs['path'] == base_path and rs.get('index'):
                    has_index = True
                    break
        if not has_index:
            return
        _dirty_by_root.setdefault(base_path, set()).add(rel_path)
        # Cancel previous timer and reset (debounce)
        if _dirty_timer is not None:
            _dirty_timer.cancel()
        _dirty_timer = threading.Timer(3.0, _run_index_update)
        _dirty_timer.daemon = True
        _dirty_timer.start()


def cancel_pending_index_updates():
    """Cancel any pending debounced index updates.

    Called when the primary project changes so that stale dirty-file
    sets from a previous project are never applied to the new one.
    """
    global _dirty_timer
    with _lock:
        if _dirty_timer is not None:
            _dirty_timer.cancel()
            _dirty_timer = None
        _dirty_by_root.clear()
    logger.debug('Cancelled all pending index updates')


def _run_index_update():
    """Background: re-index only the dirty files and merge into *their own* root's index."""
    from lib.project_mod.config import INDEX_MODEL, _roots
    from lib.project_mod.indexer import _call_llm, _extract_json, _file_hash, _save_index

    global _dirty_timer
    with _lock:
        if _state.get('indexing'):
            # Full indexing running — skip, it'll pick up changes
            _dirty_by_root.clear()
            return
        # Snapshot and clear all pending dirty entries
        pending = {bp: set(fset) for bp, fset in _dirty_by_root.items()}
        _dirty_by_root.clear()
        _dirty_timer = None

    if not pending:
        return

    for base_path, files in pending.items():
        # Resolve the correct index object for this base_path.
        # It may live in _state (primary) or in _roots (extra root).
        with _lock:
            index = None
            if _state.get('path') == base_path:
                index = _state.get('index')
            else:
                for _rn, rs in _roots.items():
                    if rs['path'] == base_path:
                        index = rs.get('index')
                        break
        if not index:
            logger.debug('Skipping dirty update for %s — no active index', base_path)
            continue

        _update_index_for_root(base_path, files, index)


def _update_index_for_root(base_path, files, index):
    """Re-index *files* for a single root and merge results into *index*."""
    from lib.project_mod.config import INDEX_MODEL
    from lib.project_mod.indexer import _call_llm, _extract_json, _file_hash, _save_index

    MAX_CHARS = 3000
    contents = []  # [(rel, text)]
    new_hashes = {}
    for rel in files:
        fp = os.path.join(base_path, rel)
        if not os.path.isfile(fp):
            # File deleted — remove from index
            index.get('files', {}).pop(rel, None)
            index.get('fileHashes', {}).pop(rel, None)
            continue
        try:
            curr_hash = _file_hash(fp)
            new_hashes[rel] = curr_hash
            with open(fp, errors='replace') as f:
                lines = []
                for _ in range(60):
                    ln = f.readline()
                    if not ln:
                        break
                    lines.append(ln)
            text = ''.join(lines)
            if len(text) > MAX_CHARS:
                text = text[:MAX_CHARS] + '\n…'
            contents.append((rel, text))
        except Exception as e:
            logger.debug('[Modifications] file read failed for %s during re-index: %s', rel, e, exc_info=True)
            contents.append((rel, '(could not read)'))

    if not contents:
        # Only deletions — save updated index
        _save_index(base_path, index)
        return

    # Call LLM to describe the changed files
    prompt = (
        'Briefly describe each file\'s purpose (1 sentence max per file). '
        'Reply ONLY with valid JSON: {"path": "description", ...}\n\n')
    for rel, text in contents:
        prompt += f'=== {rel} ===\n{text}\n\n'

    try:
        resp_text = _call_llm([{'role': 'user', 'content': prompt}], INDEX_MODEL)
        parsed = _extract_json(resp_text)
        if parsed:
            index.setdefault('files', {}).update(parsed)
            index.setdefault('fileHashes', {}).update(new_hashes)
            _save_index(base_path, index)
            logger.info('Auto-updated index for %s: %s', base_path, list(parsed.keys()))
        else:
            logger.info('Auto-update index: LLM returned no valid JSON')
    except Exception as e:
        logger.error('Auto-update index error for %s: %s', base_path, e, exc_info=True)


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
    """Save current modifications to disk."""
    session_file = os.path.join(session_dir, 'modifications.json')
    try:
        with _lock:
            mods = list(_state['modifications'])
        if mods:
            with open(session_file, 'w') as f:
                json.dump({'modifications': mods}, f, indent=2)
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

    # Schedule re-index for affected files
    affected_files = set(m['path'] for m in conv_mods)
    for f in affected_files:
        _schedule_index_update(base_path, f)

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

    # Schedule re-index for affected files
    affected_files = set(m['path'] for m in task_mods)
    for f in affected_files:
        _schedule_index_update(base_path, f)

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

