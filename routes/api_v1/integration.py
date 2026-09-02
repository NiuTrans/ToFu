"""REST surface for the deterministic project integration control plane."""

from __future__ import annotations

from quart import Blueprint

from lib.api_response import api_bad_request, api_payload
from lib.log import get_logger
from lib.openapi import api_meta
from lib.rate_limiter import rate_limit
from lib.request_parser import decode_proxy_path_arg, parse_body

from .auth import request_user_id, require_auth

logger = get_logger(__name__)
api_v1_integration_bp = Blueprint('api_v1_integration', __name__)


def _integration_api(name):
    """Load the optional Git integration control plane on first use."""
    from lib import integration_control
    return getattr(integration_control, name)


def _project_path(explicit: str = '') -> str:
    if explicit:
        return explicit
    from lib.project_mod.config import _state
    return _state.get('path', '') or ''


def _error(exc: Exception):
    if exc.__class__.__name__ in {'IntegrationError', 'IntegrationStateError'}:
        return api_bad_request(str(exc))
    logger.exception('[Integration.v1] operation failed')
    return api_payload({'ok': False, 'error': str(exc)}, 500)


@api_v1_integration_bp.route('/api/v1/project/integration/status', methods=['GET'])
@require_auth
@api_meta(summary='Deterministic integration pipeline status', tags=['project'])
def integration_status_route():
    try:
        path = _project_path(decode_proxy_path_arg('path'))
        return api_payload(_integration_api('integration_status')(
            path, user_id=int(request_user_id())))
    except Exception as exc:
        logger.debug('[Integration.v1] status request failed: %s', exc)
        return _error(exc)


@api_v1_integration_bp.route('/api/v1/project/integration/create', methods=['POST'])
@require_auth
@rate_limit(limit=30, per=60)
@api_meta(summary='Create a managed writer worktree', tags=['project'])
def integration_create_route():
    data = parse_body()
    try:
        result = _integration_api('create_workspace')(
            _project_path(str(data.get('path') or '')),
            str(data.get('taskId') or ''), str(data.get('title') or ''),
            user_id=int(request_user_id()),
        )
        return api_payload(result)
    except Exception as exc:
        logger.debug('[Integration.v1] create request failed: %s', exc)
        return _error(exc)


@api_v1_integration_bp.route('/api/v1/project/integration/register', methods=['POST'])
@require_auth
@rate_limit(limit=30, per=60)
@api_meta(summary='Register an existing writer worktree', tags=['project'])
def integration_register_route():
    data = parse_body()
    try:
        result = _integration_api('register_workspace')(
            _project_path(str(data.get('path') or '')),
            str(data.get('taskId') or ''), str(data.get('workspacePath') or ''),
            str(data.get('title') or ''), managed=False,
            user_id=int(request_user_id()),
        )
        return api_payload(result)
    except Exception as exc:
        logger.debug('[Integration.v1] register request failed: %s', exc)
        return _error(exc)


def _task_action(function):
    data = parse_body()
    try:
        result = function(
            _project_path(str(data.get('path') or '')),
            str(data.get('taskId') or ''),
            user_id=int(request_user_id()),
        )
        return api_payload(result)
    except Exception as exc:
        logger.debug('[Integration.v1] task action failed: %s', exc)
        return _error(exc)


@api_v1_integration_bp.route('/api/v1/project/integration/checkpoint', methods=['POST'])
@require_auth
@rate_limit(limit=60, per=60)
@api_meta(summary='Capture a writer checkpoint without staging its index', tags=['project'])
def integration_checkpoint_route():
    return _task_action(_integration_api('checkpoint_workspace'))


@api_v1_integration_bp.route('/api/v1/project/integration/submit', methods=['POST'])
@require_auth
@rate_limit(limit=60, per=60)
@api_meta(summary='Checkpoint and enqueue a writer workspace', tags=['project'])
def integration_submit_route():
    return _task_action(_integration_api('submit_workspace'))


@api_v1_integration_bp.route('/api/v1/project/integration/retry', methods=['POST'])
@require_auth
@rate_limit(limit=60, per=60)
@api_meta(summary='Retry an existing quarantined checkpoint', tags=['project'])
def integration_retry_route():
    return _task_action(_integration_api('retry_workspace'))


@api_v1_integration_bp.route('/api/v1/project/integration/discard', methods=['POST'])
@require_auth
@rate_limit(limit=30, per=60)
@api_meta(summary='Discard an integration workspace without deleting its refs', tags=['project'])
def integration_discard_route():
    return _task_action(_integration_api('discard_workspace'))


@api_v1_integration_bp.route('/api/v1/project/integration/promote', methods=['POST'])
@require_auth
@rate_limit(limit=20, per=60)
@api_meta(summary='Promote candidate to the known-good stable ref', tags=['project'])
def integration_promote_route():
    data = parse_body()
    try:
        return api_payload(_integration_api('promote_stable')(
            _project_path(str(data.get('path') or '')),
            user_id=int(request_user_id()),
            acknowledge_head_divergence=bool(
                data.get('acknowledgeHeadDivergence', False)),
        ))
    except Exception as exc:
        logger.debug('[Integration.v1] promote request failed: %s', exc)
        return _error(exc)


@api_v1_integration_bp.route(
    '/api/v1/project/integration/reconcile-head', methods=['POST'])
@require_auth
@rate_limit(limit=10, per=60)
@api_meta(summary='Reconcile committed canonical HEAD into candidate', tags=['project'])
def integration_reconcile_head_route():
    data = parse_body()
    try:
        return api_payload(_integration_api('reconcile_candidate_with_head')(
            _project_path(str(data.get('path') or '')),
            user_id=int(request_user_id()),
        ))
    except Exception as exc:
        logger.debug('[Integration.v1] reconcile-head request failed: %s', exc)
        return _error(exc)


@api_v1_integration_bp.route('/api/v1/project/integration/prune', methods=['POST'])
@require_auth
@rate_limit(limit=10, per=60)
@api_meta(summary='Prune stale Git worktree metadata', tags=['project'])
def integration_prune_route():
    data = parse_body()
    try:
        return api_payload(_integration_api('prune_worktree_metadata')(
            _project_path(str(data.get('path') or '')),
            user_id=int(request_user_id()),
        ))
    except Exception as exc:
        logger.debug('[Integration.v1] prune request failed: %s', exc)
        return _error(exc)


__all__ = ['api_v1_integration_bp']
