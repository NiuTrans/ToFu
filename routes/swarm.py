"""routes/swarm.py — API endpoints for agent swarm management and monitoring."""

from flask import Blueprint, jsonify

from lib.log import get_logger

logger = get_logger(__name__)

swarm_bp = Blueprint('swarm', __name__)


@swarm_bp.route('/api/swarm/status/<task_id>')
def swarm_status(task_id):
    """Return current swarm status for a task."""
    try:
        from lib.swarm.integration import get_swarm_status
        status = get_swarm_status(task_id)
        if status is None:
            return jsonify({'active': False, 'message': 'No swarm for this task'})
        return jsonify(status)
    except Exception as e:
        logger.error('Swarm status error: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500


@swarm_bp.route('/api/swarm/abort/<task_id>', methods=['POST'])
def swarm_abort(task_id):
    """Abort all sub-agents in a swarm."""
    try:
        from lib.swarm.integration import abort_swarm
        abort_swarm(task_id)
        return jsonify({'ok': True, 'message': 'Swarm abort requested'})
    except Exception as e:
        logger.error('Swarm abort error: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500


@swarm_bp.route('/api/swarm/config')
def swarm_config():
    """Return available swarm configuration info."""
    from lib.swarm.registry import AGENT_ROLES
    return jsonify({
        'available': True,
        'version': '1.0.0',
        'roles': list(AGENT_ROLES.keys()),
        'max_concurrent_agents': 5,
    })
