"""routes/api_v1/myday.py — My Day goals HTTP adapter (thin route layer).

Every rule lives in ``lib.myday``; this module decodes bodies against
``contracts/myday_goals_v1.schema.json``, resolves the authenticated owner and
maps typed failures.

Endpoints (mounted under /api/v4 by the release policy):
  GET    /api/v1/myday/goals                              — goal views
  POST   /api/v1/myday/goals                              — create a goal {text}
  PATCH  /api/v1/myday/goals/<goal_id>                    — edit text, archive, restore
  POST   /api/v1/myday/goals/<goal_id>/items              — add an owner item {text}
  PATCH  /api/v1/myday/goals/<goal_id>/items/<item_id>    — status, text, accept, follow evidence
  DELETE /api/v1/myday/goals/<goal_id>/items/<item_id>    — remove (owner) or dismiss (suggestion)
  POST   /api/v1/myday/goals/<goal_id>/suggestions        — accept or dismiss every pending suggestion
  POST   /api/v1/myday/goals/<goal_id>/decompose          — run or join the goal's decomposition
  POST   /api/v1/myday/goals/<goal_id>/link               — map to a Coordinator request
  DELETE /api/v1/myday/goals/<goal_id>/link               — remove the mapping
  GET    /api/v1/myday/requests                           — requests a goal can map to
"""
from __future__ import annotations

from quart import Blueprint

from lib.api_response import api_bad_request, api_error, api_not_found, api_ok
from lib.log import get_logger
from lib.openapi import api_meta

from .auth import request_user_id, require_auth

logger = get_logger(__name__)

api_v1_myday_bp = Blueprint('api_v1_myday', __name__)
_TAGS = ['myday']


def _service():
    from lib.myday.service import MyDayGoals
    return MyDayGoals(int(request_user_id()))


def _body(name):
    from lib.myday.contract import request_body
    return request_body(name)


def _failure(exc: Exception):
    from lib.myday.decompose import DecompositionError
    from lib.myday.goals import GoalNotFound
    from lib.storage import StorageError, http_status_for_storage_error
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, GoalNotFound):
        return api_not_found('Unknown goal')
    if isinstance(exc, DecompositionError):
        return api_error(str(exc), status=502)
    if isinstance(exc, PermissionError):
        return api_error('forbidden', status=403)
    if isinstance(exc, ValueError):
        return api_bad_request(str(exc))
    if isinstance(exc, HTTPException):
        return api_error(exc.description, status=exc.code or 500)
    if isinstance(exc, StorageError):
        return api_error(exc, status=http_status_for_storage_error(exc))
    raise exc


async def _respond(work):
    try:
        return api_ok(await work())
    except Exception as exc:
        logger.debug('[MyDay] request failed: %s', exc, exc_info=True)
        return _failure(exc)


@api_v1_myday_bp.route('/api/v1/myday/goals', methods=['GET'])
@require_auth
@api_meta(summary='Read the owner\'s My Day goals with item states and progress', tags=_TAGS)
async def myday_goals_read():
    return await _respond(lambda: _service().view())


@api_v1_myday_bp.route('/api/v1/myday/goals', methods=['POST'])
@require_auth
@api_meta(summary='Create an owner goal', tags=_TAGS, request_body=_body('createGoalRequest'))
async def myday_goal_create():
    async def work():
        from lib.myday.contract import parse
        body = await parse('createGoalRequest')
        return {'goal': await _service().write(lambda store: store.create(body['text']))}
    return await _respond(work)


@api_v1_myday_bp.route('/api/v1/myday/goals/<goal_id>', methods=['PATCH'])
@require_auth
@api_meta(summary='Edit, archive or restore a goal', tags=_TAGS, request_body=_body('updateGoalRequest'))
async def myday_goal_update(goal_id):
    async def work():
        from lib.myday.contract import parse
        body = await parse('updateGoalRequest')
        return {'goal': await _service().write(lambda store: store.edit_goal(
            goal_id, text=body.get('text'), state=body.get('state')))}
    return await _respond(work)


@api_v1_myday_bp.route('/api/v1/myday/goals/<goal_id>/items', methods=['POST'])
@require_auth
@api_meta(summary='Add an owner item to a goal', tags=_TAGS, request_body=_body('addItemRequest'))
async def myday_goal_item_add(goal_id):
    async def work():
        from lib.myday.contract import parse
        body = await parse('addItemRequest')
        return {'goal': await _service().write(lambda store: store.add_item(goal_id, body['text']))}
    return await _respond(work)


@api_v1_myday_bp.route('/api/v1/myday/goals/<goal_id>/items/<item_id>', methods=['PATCH'])
@require_auth
@api_meta(summary='Set an item status (manual), edit it, accept a suggestion or follow Worker evidence',
          tags=_TAGS, request_body=_body('updateItemRequest'))
async def myday_goal_item_update(goal_id, item_id):
    async def work():
        from lib.myday.contract import parse
        body = await parse('updateItemRequest')
        return {'goal': await _service().update_item(goal_id, item_id, body)}
    return await _respond(work)


@api_v1_myday_bp.route('/api/v1/myday/goals/<goal_id>/items/<item_id>', methods=['DELETE'])
@require_auth
@api_meta(summary='Remove an owner item or dismiss a suggestion', tags=_TAGS, request_body=False)
async def myday_goal_item_remove(goal_id, item_id):
    return await _respond(lambda: _goal(_service().write(lambda store: store.remove_item(goal_id, item_id))))


@api_v1_myday_bp.route('/api/v1/myday/goals/<goal_id>/suggestions', methods=['POST'])
@require_auth
@api_meta(summary='Accept or dismiss every pending suggestion of a goal', tags=_TAGS,
          request_body=_body('suggestionsRequest'))
async def myday_goal_suggestions(goal_id):
    async def work():
        from lib.myday.contract import parse
        body = await parse('suggestionsRequest')
        return {'goal': await _service().write(lambda store: store.decide_suggestions(goal_id, body['decision']))}
    return await _respond(work)


@api_v1_myday_bp.route('/api/v1/myday/goals/<goal_id>/decompose', methods=['POST'])
@require_auth
@api_meta(summary='Run or join the decomposition of a goal into suggested items', tags=_TAGS,
          request_body=False)
async def myday_goal_decompose(goal_id):
    return await _respond(lambda: _goal(_service().decompose(goal_id)))


@api_v1_myday_bp.route('/api/v1/myday/goals/<goal_id>/link', methods=['POST'])
@require_auth
@api_meta(summary='Map a goal to one of the owner\'s Coordinator requests', tags=_TAGS,
          request_body=_body('linkRequest'))
async def myday_goal_link(goal_id):
    async def work():
        from lib.myday.contract import parse
        body = await parse('linkRequest')
        return {'goal': await _service().link(goal_id, body['projectPath'], body['sourceAttemptId'])}
    return await _respond(work)


@api_v1_myday_bp.route('/api/v1/myday/goals/<goal_id>/link', methods=['DELETE'])
@require_auth
@api_meta(summary='Remove a goal\'s request mapping', tags=_TAGS, request_body=False)
async def myday_goal_unlink(goal_id):
    return await _respond(lambda: _goal(_service().write(lambda store: store.unlink(goal_id))))


@api_v1_myday_bp.route('/api/v1/myday/requests', methods=['GET'])
@require_auth
@api_meta(summary='List the owner\'s recent Coordinator requests a goal can map to', tags=_TAGS)
async def myday_requests_read():
    return await _respond(lambda: _service().linkable_requests())


async def _goal(pending):
    return {'goal': await pending}


__all__ = ['api_v1_myday_bp']
