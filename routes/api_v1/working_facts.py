"""Owner-scoped working-fact reads/proposals and explicit owner decisions."""
from quart import Blueprint, request
from quart.utils import run_sync

from lib.api_response import (api_bad_request, api_conflict, api_forbidden,
                              api_not_found, api_ok, api_payload_too_large)
from lib.memory.working_facts import service
from lib.memory.working_facts.contract import contract, limit, validate
from lib.memory.working_facts.repository import FactConflict, FactsUnavailable
from lib.openapi import api_meta, schema_definition
from lib.request_parser import async_parse_body
from .auth import request_principal, require_scope

api_v1_working_facts_bp = Blueprint('api_v1_working_facts', __name__)
_PATH = '/api/v1/conversations/<conv_id>/working-facts'
_RESPONSES = {'200': {'description': 'Versioned working facts and source bindings', 'content': {
    'application/json': {'schema': schema_definition(contract(), 'WorkingFactsView')}}}}


@api_v1_working_facts_bp.route(_PATH, methods=['GET'])
@require_scope('chat')
@api_meta(summary='Read conversation working facts', tags=['memory'], scope='chat', responses=_RESPONSES)
def read_working_facts_v1(conv_id):
    try:
        return api_ok(service.query_facts(conv_id, principal=request_principal()))
    except FactsUnavailable:
        return api_not_found('not_found')


@api_v1_working_facts_bp.route(_PATH, methods=['POST'])
@require_scope('chat')
@api_meta(summary='Propose, correct, confirm or retract a conversation fact', tags=['memory'], scope='chat',
          responses=_RESPONSES, request_body={'required': True, 'content': {'application/json': {
              'schema': schema_definition(contract(), 'WorkingFactCommand')}}})
async def update_working_facts_v1(conv_id):
    if len(await request.get_data()) > limit('maxRequestBytes'):
        return api_payload_too_large(limit('maxRequestBytes'))
    body = await async_parse_body(strict=True, exact=tuple({field for branch in contract()['$defs']['WorkingFactCommand']['anyOf']
        for field in contract()['$defs'][branch['$ref'].rsplit('/', 1)[1]]['properties']}))
    try:
        validate(body, 'WorkingFactCommand')
        operation = service.record_fact if body['action'] == 'record' else service.owner_decision
        return api_ok(await run_sync(operation)(conv_id, body, principal=request_principal()))
    except FactConflict as exc:
        return api_conflict(str(exc))
    except FactsUnavailable:
        return api_not_found('not_found')
    except PermissionError as exc:
        return api_forbidden(str(exc))
    except ValueError as exc:
        return api_bad_request(str(exc))


@api_v1_working_facts_bp.route(_PATH + '/consumers', methods=['GET'])
@require_scope('chat')
@api_meta(summary='Read tasks dependent on conversation working facts', tags=['memory'], scope='chat',
          responses={'200': {'description': 'Versioned task dependency assessment', 'content': {
              'application/json': {'schema': schema_definition(contract(), 'WorkingFactConsumersView')}}}})
def read_working_fact_consumers_v1(conv_id):
    from lib.memory.working_facts import consumers
    try:
        raw_limit = request.args.get('limit')
        return api_ok(consumers.query(conv_id, principal=request_principal(),
            cursor=request.args.get('cursor', ''), page_size=int(raw_limit) if raw_limit is not None else None))
    except FactsUnavailable:
        return api_not_found('not_found')
    except ValueError as exc:
        return api_bad_request(str(exc))
