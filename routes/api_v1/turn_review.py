"""Authenticated HTTP adapter for ordinary Turn owner acceptance.

The shared acceptance service owns requirements, evidence and output scope;
this adapter only validates HTTP and translates typed domain failures.
"""
from quart import Blueprint, request
from quart.utils import run_sync

from lib.acceptance.contract import contract
from lib.acceptance.review import ReviewConflict
from lib.acceptance.turns import read_review, update_review
from lib.openapi import api_meta, schema_definition
from lib.api_response import (api_ok, api_not_found, api_bad_request, api_conflict,
                              api_forbidden, api_payload_too_large)
from lib.artifacts.core import ArtifactNotFoundError
from lib.turn_lifecycle import LifecycleNotFound
from lib.request_parser import async_parse_body
from .auth import request_principal, require_scope

api_v1_turn_review_bp = Blueprint('api_v1_turn_review', __name__)


def _responses():
    return {'200': {'description': 'Turn acceptance and declared output coverage', 'content': {
        'application/json': {'schema': schema_definition(contract(), 'TurnReviewView')},
    }}}


@api_v1_turn_review_bp.route('/api/v1/conversations/<conversation_id>/turns/<turn_id>/review', methods=['GET'])
@require_scope('chat')
@api_meta(summary='Read ordinary Turn owner acceptance', tags=['conversations'],
          scope='chat', responses=_responses())
async def read_turn_review_v1(conversation_id, turn_id):
    try:
        return api_ok(await run_sync(read_review)(conversation_id, turn_id, principal=request_principal()))
    except LifecycleNotFound:
        return api_not_found('not_found')


@api_v1_turn_review_bp.route('/api/v1/conversations/<conversation_id>/turns/<turn_id>/review', methods=['POST'])
@require_scope('chat')
@api_meta(summary='Save Turn requirements or an owner judgment of declared output versions',
          tags=['conversations'], scope='chat', responses=_responses(), request_body={
              'required': True, 'content': {'application/json': {
                  'schema': schema_definition(contract(), 'TurnReviewCommand'),
              }}})
async def update_turn_review_v1(conversation_id, turn_id):
    max_bytes = contract()['x-limits']['maxRequestBytes']
    if len(await request.get_data()) > max_bytes:
        return api_payload_too_large(max_bytes)
    definitions = contract()['$defs']
    fields = {field for branch in definitions['TurnReviewCommand']['anyOf']
              for field in definitions[branch['$ref'].rsplit('/', 1)[1]]['properties']}
    body = await async_parse_body(strict=True, exact=tuple(fields))
    try:
        return api_ok(await run_sync(update_review)(conversation_id, turn_id, body,
                                                   principal=request_principal()))
    except (LifecycleNotFound, ArtifactNotFoundError):
        return api_not_found('not_found')
    except ReviewConflict as exc:
        return api_conflict(str(exc))
    except PermissionError as exc:
        return api_forbidden(str(exc))
    except ValueError as exc:
        return api_bad_request(str(exc))
