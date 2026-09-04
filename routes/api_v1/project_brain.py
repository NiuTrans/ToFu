"""Signal-driven Project Brain HTTP projection and human-command surface."""

from __future__ import annotations

from quart import Blueprint

from lib.api_response import api_bad_request, api_error, api_internal_error, api_ok
from lib.log import get_logger
from lib.openapi import api_meta
from lib.rate_limiter import rate_limit
from lib.request_parser import decode_proxy_path_arg, parse_body

from .auth import request_user_id, require_auth


logger = get_logger(__name__)
api_v1_project_brain_bp = Blueprint('api_v1_project_brain', __name__)


def _path(explicit: str = '') -> str:
    path = str(explicit or decode_proxy_path_arg('path') or '').strip()
    if not path:
        raise ValueError('path is required')
    return path


def _owner() -> int:
    return int(request_user_id())


def _error(exc: Exception):
    if isinstance(exc, ValueError):
        return api_bad_request(str(exc))
    from lib.storage import StorageError, http_status_for_storage_error
    if isinstance(exc, StorageError):
        return api_error(exc, status=http_status_for_storage_error(exc))
    logger.exception('[ProjectBrain.v1] request failed')
    return api_internal_error(
        exc, context='project:brain', source='routes.api_v1.project_brain',
        log_traceback=False,
    )


@api_v1_project_brain_bp.route('/api/v1/project/board', methods=['GET'])
@require_auth
@api_meta(summary='Read automatic project work items', tags=['project'])
def project_brain_board():
    try:
        from lib.conversations.project_brain import board_projection
        return api_ok(board_projection(_path(), user_id=_owner()))
    except Exception as exc:
        return _error(exc)


@api_v1_project_brain_bp.route('/api/v1/project/feed', methods=['GET'])
@require_auth
@api_meta(summary='Read important project narrative events', tags=['project'])
def project_brain_feed():
    try:
        from quart import request
        from lib.conversations.project_brain import feed_projection
        since = int(request.args.get('since') or 0)
        limit = int(request.args.get('limit') or 100)
        return api_ok(feed_projection(
            _path(), user_id=_owner(), since_sequence=since, limit=limit))
    except Exception as exc:
        return _error(exc)


@api_v1_project_brain_bp.route('/api/v1/project/charter', methods=['GET'])
@require_auth
@api_meta(summary='Read executable checker-backed Charter', tags=['project'])
def project_brain_charter():
    try:
        from lib.conversations.project_brain import charter_projection
        return api_ok(charter_projection(_path(), user_id=_owner()))
    except Exception as exc:
        return _error(exc)


@api_v1_project_brain_bp.route('/api/v1/project/brain/status', methods=['GET'])
@require_auth
@api_meta(summary='Read derived Project Brain status', tags=['project'])
def project_brain_status():
    try:
        from lib.conversations.project_brain import status_projection
        return api_ok(status_projection(_path(), user_id=_owner()))
    except Exception as exc:
        return _error(exc)


@api_v1_project_brain_bp.route('/api/v1/project/brain/attention', methods=['GET'])
@require_auth
@api_meta(summary='Read Project Brain attention items', tags=['project'])
def project_brain_attention():
    try:
        from lib.conversations.project_brain import attention_projection
        return api_ok(attention_projection(_path(), user_id=_owner()))
    except Exception as exc:
        return _error(exc)


@api_v1_project_brain_bp.route(
    '/api/v1/project/brain/attention/add', methods=['POST'])
@require_auth
@rate_limit(limit=30, per=60)
@api_meta(summary='Save a human-selected pending decision for triage', tags=['project'])
def project_brain_attention_add():
    data = parse_body()
    try:
        from lib.conversations.project_brain import add_attention
        text = str(data.get('text') or '').strip()
        if not text:
            raise ValueError('text is required')
        result = add_attention(
            _path(str(data.get('path') or '')),
            kind='pending_decision', text=text, user_id=_owner())
        return api_ok({'attention': result})
    except Exception as exc:
        return _error(exc)


@api_v1_project_brain_bp.route('/api/v1/project/brain/summary', methods=['GET'])
@require_auth
@api_meta(summary='Read compact Project Brain projection summary', tags=['project'])
def project_brain_summary():
    try:
        from lib.conversations.project_brain import (
            attention_projection, board_projection, charter_projection,
            status_projection, watch_projection,
        )
        path = _path()
        user_id = _owner()
        return api_ok({
            'board': board_projection(path, user_id=user_id),
            'status': status_projection(path, user_id=user_id),
            'attention': attention_projection(path, user_id=user_id),
            'charter': charter_projection(path, user_id=user_id),
            'watch': watch_projection(path, user_id=user_id),
        })
    except Exception as exc:
        return _error(exc)


@api_v1_project_brain_bp.route('/api/v1/project/brain/watch', methods=['GET'])
@require_auth
@api_meta(summary='Read project Watch items', tags=['project'])
def project_brain_watch():
    try:
        from lib.conversations.project_brain import watch_projection
        return api_ok(watch_projection(_path(), user_id=_owner()))
    except Exception as exc:
        return _error(exc)


@api_v1_project_brain_bp.route('/api/v1/project/brain/watch/add', methods=['POST'])
@require_auth
@rate_limit(limit=60, per=60)
@api_meta(summary='Add a human-maintained Watch item', tags=['project'])
def project_brain_watch_add():
    data = parse_body()
    try:
        from lib.conversations.project_brain import add_watch_item
        return api_ok({'item': add_watch_item(
            _path(str(data.get('path') or '')),
            kind=str(data.get('kind') or 'concern'),
            text=str(data.get('text') or ''), user_id=_owner(),
            source_conversation_id=str(data.get('conversationId') or ''),
        )})
    except Exception as exc:
        return _error(exc)


@api_v1_project_brain_bp.route('/api/v1/project/brain/watch/update', methods=['POST'])
@require_auth
@rate_limit(limit=120, per=60)
@api_meta(summary='Update a human-maintained Watch item', tags=['project'])
def project_brain_watch_update():
    data = parse_body()
    try:
        from lib.conversations.project_brain import update_watch_item
        return api_ok({'item': update_watch_item(
            _path(str(data.get('path') or '')),
            str(data.get('itemId') or ''), user_id=_owner(),
            text=data.get('text'), status=data.get('status'),
            latest_result=data.get('latestResult'),
        )})
    except Exception as exc:
        return _error(exc)


@api_v1_project_brain_bp.route('/api/v1/project/brain/watch/delete', methods=['POST'])
@require_auth
@rate_limit(limit=60, per=60)
@api_meta(summary='Delete a human-maintained Watch item', tags=['project'])
def project_brain_watch_delete():
    data = parse_body()
    try:
        from lib.conversations.project_brain import delete_watch_item
        delete_watch_item(
            _path(str(data.get('path') or '')),
            str(data.get('itemId') or ''), user_id=_owner())
        return api_ok({'deleted': True})
    except Exception as exc:
        return _error(exc)


@api_v1_project_brain_bp.route('/api/v1/project/brain/checkers', methods=['GET'])
@require_auth
@api_meta(summary='List versioned project checker definitions', tags=['project'])
def project_brain_checkers():
    try:
        from lib.conversations.project_brain import checker_catalog
        return api_ok(checker_catalog(_path(), user_id=_owner()))
    except Exception as exc:
        return _error(exc)


@api_v1_project_brain_bp.route(
    '/api/v1/project/brain/checkers/register', methods=['POST'])
@require_auth
@rate_limit(limit=30, per=60)
@api_meta(summary='Register an immutable checker version', tags=['project'])
def project_brain_checker_register():
    data = parse_body()
    try:
        from lib.conversations.project_brain import register_checker
        definition = data.get('definition')
        if not isinstance(definition, dict):
            raise ValueError('definition is required')
        return api_ok({'checker': register_checker(
            _path(str(data.get('path') or '')), definition,
            user_id=_owner())})
    except Exception as exc:
        return _error(exc)


@api_v1_project_brain_bp.route(
    '/api/v1/project/brain/checkers/run', methods=['POST'])
@require_auth
@rate_limit(limit=30, per=60)
@api_meta(summary='Run one registered checker version', tags=['project'])
def project_brain_checker_run():
    data = parse_body()
    try:
        from lib.conversations.project_brain import run_checker
        return api_ok({'result': run_checker(
            _path(str(data.get('path') or '')),
            str(data.get('checkerId') or ''),
            int(data.get('version') or 0), user_id=_owner(),
            work_id=str(data.get('workId') or ''), reason='manual',
        )})
    except Exception as exc:
        return _error(exc)


@api_v1_project_brain_bp.route(
    '/api/v1/project/charter/decision/promote', methods=['POST'])
@require_auth
@rate_limit(limit=30, per=60)
@api_meta(summary='Promote an assistant conclusion with a checker', tags=['project'])
def project_brain_decision_promote():
    data = parse_body()
    checker_ref = data.get('checkerRef')
    if not isinstance(checker_ref, dict):
        return api_bad_request('checkerRef is required', field='checkerRef')
    try:
        from lib.conversations.project_brain import promote_decision
        decision = promote_decision(
            _path(str(data.get('path') or '')),
            decision_id=str(data.get('decisionId') or ''),
            text=str(data.get('text') or ''),
            checker_id=str(checker_ref.get('id') or ''),
            checker_version=int(checker_ref.get('version') or 0),
            source_conversation_id=str(data.get('sourceConversationId') or ''),
            source_turn_id=str(data.get('sourceTurnId') or ''),
            user_id=_owner(),
        )
        return api_ok({'decision': decision})
    except Exception as exc:
        return _error(exc)


__all__ = ['api_v1_project_brain_bp']
