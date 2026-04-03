"""routes/project.py — Project Co-Pilot API (set, status, browse, write approval)."""

from flask import Blueprint, jsonify, request

from lib.log import get_logger
from lib.rate_limiter import rate_limit

logger = get_logger(__name__)

project_bp = Blueprint('project', __name__)


@project_bp.route('/api/project/set', methods=['POST'])
@rate_limit(limit=10, per=60)  # 10 requests per minute
def project_set():
    data = request.get_json(silent=True) or {}
    path = data.get('path', '').strip()
    if not path:
        return jsonify({'error': 'No path provided'}), 400
    try:
        from lib.project_mod import set_project
        state = set_project(path)
        return jsonify({'ok': True, **state})
    except Exception as e:
        logger.error('[Project] set_project failed for path %s: %s', path, e, exc_info=True)
        return jsonify({'error': str(e)}), 400


@project_bp.route('/api/project/set_paths', methods=['POST'])
@rate_limit(limit=10, per=60)  # 10 requests per minute
def project_set_paths():
    """Atomically set multiple project paths.
    Body: { "paths": ["/primary", "/extra1", "/extra2", …] }
    First path = primary project, rest = extra workspace roots.
    """
    data = request.get_json(silent=True) or {}
    paths = data.get('paths', [])
    if not paths or not isinstance(paths, list):
        return jsonify({'error': 'Provide a "paths" array with at least one directory'}), 400
    try:
        from lib.project_mod import set_project_paths
        state = set_project_paths(paths)
        return jsonify({'ok': True, **state})
    except Exception as e:
        logger.error('[Project] set_project_paths failed for %s: %s', paths, e, exc_info=True)
        return jsonify({'error': str(e)}), 400


@project_bp.route('/api/project/index', methods=['POST'])
@rate_limit(limit=5, per=300)  # 5 requests per 5 minutes
def project_index():
    """Trigger AI-powered semantic indexing of the current project."""
    from lib.project_mod import start_indexing
    try:
        result = start_indexing()
        return jsonify(result)
    except Exception as e:
        logger.error('[Project] Indexing failed: %s', e, exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@project_bp.route('/api/project/status', methods=['GET'])
def project_status():
    from lib.project_mod import get_state
    return jsonify(get_state())


@project_bp.route('/api/project/clear', methods=['POST'])
def project_clear():
    from lib.project_mod import clear_project
    clear_project()
    return jsonify({'ok': True})


@project_bp.route('/api/project/browse', methods=['POST'])
def project_browse():
    data = request.get_json(silent=True) or {}
    path = data.get('path', '').strip() or None
    show_hidden = data.get('showHidden', False)
    from lib.project_mod import browse_directory
    result = browse_directory(path, show_hidden=show_hidden)
    if result.get('error'):
        return jsonify(result), 400
    return jsonify(result)


@project_bp.route('/api/project/recent', methods=['GET', 'POST', 'DELETE'])
def project_recent():
    from lib.project_mod import clear_recent_projects, get_recent_projects, save_recent_project
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        path = data.get('path', '').strip()
        if path:
            save_recent_project(path)
        return jsonify({'ok': True})
    elif request.method == 'DELETE':
        clear_recent_projects()
        return jsonify({'ok': True})
    return jsonify({'projects': get_recent_projects()})


@project_bp.route('/api/project/write_approval', methods=['POST'])
def project_write_approval():
    data = request.get_json(silent=True) or {}
    approval_id = data.get('approvalId', '')
    approved = data.get('approved', False)
    if not approval_id:
        return jsonify({'error': 'No approvalId'}), 400
    from lib.tasks_pkg import resolve_write_approval
    ok = resolve_write_approval(approval_id, approved)
    if not ok:
        return jsonify({'error': 'Approval not found or expired'}), 404
    return jsonify({'ok': True, 'approved': approved})


@project_bp.route('/api/project/undo', methods=['POST'])
def project_undo():
    """Undo file modifications for a specific task (per-round undo).

    Body: { "taskId": "abc123", "convId": "...", "projectPath": "..." }
    taskId  → undo only that round's changes (preferred)
    convId  → undo the ENTIRE conversation's changes (legacy fallback)
    """
    data = request.get_json(silent=True) or {}
    task_id = data.get('taskId', '').strip()
    conv_id = data.get('convId', '').strip()
    project_path = data.get('projectPath', '').strip()

    if not project_path:
        from lib.project_mod.config import _state
        project_path = _state.get('path', '')
    if not project_path:
        return jsonify({'error': 'No active project'}), 400

    try:
        if task_id:
            from lib.project_mod import undo_task_modifications
            result = undo_task_modifications(project_path, task_id)
            logger.info('[Project] undo task=%s: undone=%s failed=%s',
                        task_id[:8], result.get('undone', 0), result.get('failed', 0))
        elif conv_id:
            from lib.project_mod import undo_conv_modifications
            result = undo_conv_modifications(project_path, conv_id)
            logger.info('[Project] undo conv=%s: undone=%s failed=%s',
                        conv_id[:8], result.get('undone', 0), result.get('failed', 0))
        else:
            return jsonify({'error': 'Provide taskId or convId'}), 400
        return jsonify(result)
    except Exception as e:
        logger.error('[Project] undo failed: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500


@project_bp.route('/api/project/undo_all', methods=['POST'])
def project_undo_all():
    """Undo ALL file modifications across all conversations for the active project.

    Body: { "projectPath": "..." }  (optional — uses active project if omitted)
    """
    data = request.get_json(silent=True) or {}
    project_path = data.get('projectPath', '').strip()

    if not project_path:
        from lib.project_mod.config import _state
        project_path = _state.get('path', '')
    if not project_path:
        return jsonify({'error': 'No active project'}), 400

    try:
        from lib.project_mod import undo_all_modifications
        result = undo_all_modifications(project_path)
        logger.info('[Project] undo_all: undone=%s failed=%s',
                    result.get('undone', 0), result.get('failed', 0))
        return jsonify(result)
    except Exception as e:
        logger.error('[Project] undo_all failed: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500


@project_bp.route('/api/project/rescan', methods=['POST'])
def project_rescan():
    """Re-scan the current project to refresh file tree and stats."""
    try:
        from lib.project_mod import rescan
        result = rescan()
        return jsonify({'ok': True, **(result or {})})
    except Exception as e:
        logger.error('[Project] rescan failed: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500
