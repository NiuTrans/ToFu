"""Automation management delivery; mounted through the common v4 projection.

Field authority: automation_management_v1.schema.json. Authentication is the
shared scope boundary; the application service owns revisions and occurrences.
"""
from quart import Blueprint, request
from quart.utils import run_sync

from lib.api_response import api_error, api_ok
from lib.automations.errors import AutomationConflict, AutomationError
from lib.automations.management import AutomationManagement
from lib.automations.management_contract import fragment, limits
from lib.http_body_policy import HttpBodyPolicy, enforce_http_body_policy
from lib.openapi import api_meta
from lib.request_parser import async_parse_body
from lib.storage.errors import StorageError, http_status_for_storage_error
from .auth import request_principal, require_scope

api_v1_automations_bp = Blueprint('api_v1_automations', __name__)


def service_for(principal):
    return AutomationManagement(principal)


def _response(name):
    return {'200': {'description': 'Owned automation result',
                    'content': {'application/json': {'schema': fragment(name)}}}}


def _body(name):
    return {'required': True, 'content': {'application/json': {'schema': fragment(name)}}}


def _page(cursor):
    return [{'in': 'query', 'name': 'limit', 'schema': {
        'type': 'integer', 'minimum': 1, 'maximum': limits()['pageRows'], 'default': 8}},
        {'in': 'query', 'name': cursor, 'schema': {'type': 'string', 'maxLength': 256}}]


def _pagination(cursor):
    if set(request.args) - {'limit', cursor} or any(len(request.args.getlist(key)) != 1 for key in request.args):
        raise AutomationError('invalid_pagination', 'Invalid automation page query')
    raw = request.args.get('limit', '8')
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 2:
        raise AutomationError('invalid_page_limit', 'Invalid automation page limit')
    return {'limit': int(raw), cursor: request.args.get(cursor, '')}


async def _invoke(operation, *args, body=False, cursor=None, **kwargs):
    try:
        if body:
            policy = HttpBodyPolicy(body_timeout=60, upload_body_timeout=60,
                                    route_caps=(), default_cap=limits()['requestBytes'])
            rejected = await enforce_http_body_policy(policy)
            if rejected is not None:
                return rejected
            args = (*args, await async_parse_body(strict=True))
        if cursor:
            kwargs.update(_pagination(cursor))
        service = service_for(request_principal())
        result = await run_sync(getattr(service, operation))(*args, **kwargs)
        return api_ok(result)
    except PermissionError as exc:
        return api_error(str(exc), status=403, kind='forbidden', error_code='forbidden')
    except StorageError as exc:
        return api_error(str(exc), status=http_status_for_storage_error(exc),
                         kind=exc.code, error_code=exc.code)
    except AutomationError as exc:
        status = (409 if isinstance(exc, AutomationConflict) else
                  404 if exc.code == 'automation_not_found' else
                  503 if exc.code == 'automation_project_unavailable' else
                  500 if exc.code == 'automation_storage_integrity' else 400)
        return api_error(str(exc), status=status, kind=exc.code, error_code=exc.code, path=exc.path)


@api_v1_automations_bp.get('/api/v1/automations/skill-source')
@require_scope('automations:read')
@api_meta(summary='Read owned skill provider configuration', tags=['automations'],
          scope='automations:read', responses=_response('SkillSource'))
async def skill_source():
    return await _invoke('skill_source')


@api_v1_automations_bp.put('/api/v1/automations/skill-source')
@require_scope('automations:manage')
@api_meta(summary='Configure owned skill provider credentials', tags=['automations'],
          scope='automations:manage', request_body=_body('SkillSourceUpdateRequest'), responses=_response('SkillSource'))
async def update_skill_source():
    return await _invoke('update_skill_source', body=True)


@api_v1_automations_bp.get('/api/v1/automations/actions')
@require_scope('automations:read')
@api_meta(summary='List admitted automation action contracts', tags=['automations'],
          scope='automations:read', responses=_response('ActionPage'))
async def actions():
    return await _invoke('actions')


@api_v1_automations_bp.get('/api/v1/automations/definitions')
@require_scope('automations:read')
@api_meta(summary='Page owned automation definitions', tags=['automations'],
          scope='automations:read', parameters=_page('after'), responses=_response('DefinitionPage'))
async def definitions():
    return await _invoke('definitions', cursor='after')


@api_v1_automations_bp.get('/api/v1/automations/definitions/<entity_id>')
@require_scope('automations:read')
@api_meta(summary='Read an owned automation definition', tags=['automations'],
          scope='automations:read', responses=_response('Definition'))
async def definition(entity_id):
    return await _invoke('definition', entity_id)


@api_v1_automations_bp.put('/api/v1/automations/definitions/<entity_id>')
@require_scope('automations:read', 'automations:manage')
@api_meta(summary='Atomically publish an approved automation revision', tags=['automations'],
          scope='automations:manage', request_body=_body('PublishRequest'), responses=_response('Definition'))
async def publish(entity_id):
    return await _invoke('publish', entity_id, body=True)


@api_v1_automations_bp.post('/api/v1/automations/definitions/<entity_id>/pause')
@require_scope('automations:read', 'automations:manage')
@api_meta(summary='Pause future automatic occurrences', tags=['automations'], scope='automations:manage',
          request_body=_body('RevisionRequest'), responses=_response('Definition'))
async def pause(entity_id):
    return await _invoke('inactive', entity_id, body=True)


@api_v1_automations_bp.delete('/api/v1/automations/definitions/<entity_id>')
@require_scope('automations:read', 'automations:manage')
@api_meta(summary='Retire an owned automation without deleting accepted runs', tags=['automations'],
          scope='automations:manage', request_body=_body('RevisionRequest'), responses=_response('Definition'))
async def retire(entity_id):
    return await _invoke('inactive', entity_id, body=True, retire=True)


@api_v1_automations_bp.post('/api/v1/automations/definitions/<entity_id>/runs')
@require_scope('automations:read', 'automations:execute')
@api_meta(summary='Admit or recover one manual occurrence of an exact revision', tags=['automations'],
          scope='automations:execute', request_body=_body('StartRequest'), responses=_response('Run'))
async def start(entity_id):
    return await _invoke('start', entity_id, body=True)


@api_v1_automations_bp.get('/api/v1/automations/definitions/<entity_id>/runs')
@require_scope('automations:read')
@api_meta(summary='Page recent runs newest first', tags=['automations'], scope='automations:read',
          parameters=_page('before'), responses=_response('RunPage'))
async def history(entity_id):
    return await _invoke('history', entity_id, cursor='before')


@api_v1_automations_bp.get('/api/v1/automations/runs/<entity_id>')
@require_scope('automations:read')
@api_meta(summary='Read one owned automation run', tags=['automations'],
          scope='automations:read', responses=_response('Run'))
async def run(entity_id):
    return await _invoke('run', entity_id)


@api_v1_automations_bp.post('/api/v1/automations/runs/<entity_id>/cancel')
@require_scope('automations:read', 'automations:manage')
@api_meta(summary='Record owned run cancellation intent', tags=['automations'], scope='automations:manage',
          request_body=_body('CancelRequest'), responses=_response('Run'))
async def cancel(entity_id):
    return await _invoke('cancel', entity_id, body=True)


@api_v1_automations_bp.post('/api/v1/automations/runs/<entity_id>/approve')
@require_scope('automations:read', 'automations:manage', 'automations:execute')
@api_meta(summary='Approve one reviewed automation step and wake its owned run', tags=['automations'],
          scope='automations:manage', request_body=_body('ApproveRequest'), responses=_response('Run'))
async def approve(entity_id):
    return await _invoke('approve', entity_id, body=True)


@api_v1_automations_bp.get('/api/v1/automations/conversations/<conversation_id>/attempts/<attempt_id>/presentations')
@require_scope('automations:read')
@api_meta(summary='Read owned task-start presentations from immutable run outputs', tags=['automations'],
          scope='automations:read', parameters=_page('after'), responses=_response('PresentationPage'))
async def presentations(conversation_id, attempt_id):
    return await _invoke('presentations', conversation_id, attempt_id, cursor='after')


@api_v1_automations_bp.get('/api/v1/automations/projects')
@require_scope('automations:read')
@api_meta(summary='Page owned automation project bindings',tags=['automations'],
          scope='automations:read',parameters=_page('after'),responses=_response('ProjectPage'))
async def projects():
    return await _invoke('projects',cursor='after')


@api_v1_automations_bp.get('/api/v1/automations/projects/<entity_id>')
@require_scope('automations:read')
@api_meta(summary='Read an owned automation project binding',tags=['automations'],
          scope='automations:read',responses=_response('Project'))
async def project(entity_id):
    return await _invoke('project',entity_id)


@api_v1_automations_bp.put('/api/v1/automations/projects')
@require_scope('automations:manage')
@api_meta(summary='Register or revise an owned automation project binding',tags=['automations'],
          scope='automations:manage',request_body=_body('ProjectUpdateRequest'),responses=_response('Project'))
async def update_project():
    return await _invoke('update_project',body=True)
