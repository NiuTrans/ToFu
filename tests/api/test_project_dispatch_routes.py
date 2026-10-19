"""Stateless native route wiring; grants execute through the domain service."""
import asyncio
from copy import deepcopy
import inspect
from types import SimpleNamespace

import pytest
from quart import Quart
from jsonschema import Draft202012Validator

from lib.identity import PrincipalContext
from lib.project_dispatch.contract import schema
from lib.project_dispatch.repository import CONTRACT

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('view,action,definition', [
    ('project_assignment_create', 'authorize_and_dispatch', 'request'),
    ('project_assignment_dispatch', 'dispatch', 'retry'),
    ('project_assignment_revoke', 'revoke', 'revoke'),
    ('project_assignment_review', 'review', 'reviewRequest'),
])
def test_assignment_routes_delegate_exact_principal_scope_and_request_contract(monkeypatch, view, action, definition):
    from routes.api_v1 import project_brain as routes
    from lib.project_dispatch import contract
    principal = PrincipalContext.user(subject_id='synthetic', owner_user_id=7,
                                      tenant_id='tenant-a', scopes={'chat'})
    bodies = {
        'request': {'projectPath': '/project', 'coordinatorId': 'coordinator', 'recipientId': 'recipient',
            'authorizationSource': {'conversationId': 'coordinator', 'turnId': 'source', 'projectionRevision': 1},
            'brief': 'Consult only', 'model': 'model-a',
            'budget': {'maxTokens': 100, 'maxModelRequests': 1, 'deadline': 2000}},
        'retry': {'projectPath': '/project', 'assignmentId': 'assignment'},
        'revoke': {'projectPath': '/project', 'assignmentId': 'assignment', 'expectedRevision': 1},
        'reviewRequest': {'projectPath': '/project', 'assignmentId': 'assignment', 'expectedRevision': 1,
                          'decision': 'accept', 'note': 'verified', 'resultRevision': 3},
    }
    calls = []
    def delegate(*args, **kwargs):
        calls.append((args, kwargs))
        assert kwargs['principal'] is principal
        if action == 'authorize_and_dispatch': assert kwargs['command_id'] == 'stable-command'
        return {'assignmentId': 'assignment', 'canonicalDelivery': False}
    service = SimpleNamespace(**{action: delegate})
    monkeypatch.setattr(routes, '_dispatch_service', lambda path: (service, principal))
    monkeypatch.setattr(routes, 'api_ok', lambda value: value)
    monkeypatch.setattr(contract, 'parse', lambda name: deepcopy(bodies[name]))
    handler = getattr(routes, view)
    assert handler._api_meta['scope'] == 'chat'
    declared = handler._api_meta['request_body']['content']['application/json']['schema']
    assert declared == schema(definition)
    assert Draft202012Validator(declared).is_valid(bodies[definition])
    async def run():
        app = Quart(__name__)
        async with app.test_request_context('/', headers={'Idempotency-Key': 'stable-command'}):
            return inspect.unwrap(handler)()
    result = asyncio.run(run())
    assert result['canonicalDelivery'] is False and len(calls) == 1


def test_derived_dispatch_schemas_are_strict_and_have_no_external_refs():
    for name in ('request', 'retry', 'revoke', 'reviewRequest', 'view'):
        value = schema(name)
        Draft202012Validator.check_schema(value)
        assert value['additionalProperties'] is False
        assert '$ref' not in str(value)
    assert CONTRACT['identityConfigKey'] == '_projectDispatchAdmission'


def test_permission_denial_is_not_internal_error(monkeypatch):
    from routes.api_v1 import project_brain as routes
    monkeypatch.setattr(routes, 'api_error', lambda value, status: (value, status))
    assert routes._safe_route_error_response(PermissionError('private context')) == ('forbidden', 403)


@pytest.mark.parametrize('suffix,expected', [('', {}), ('&commandId=grant-command', {'command_id': 'grant-command'}),
    ('&cursor=older/owned-cursor', {'cursor': 'older/owned-cursor'}), ('&assignmentId=retained', {'assignment_id': 'retained'})])
def test_get_command_receipt_delegates_exact_query_without_authorize(monkeypatch, suffix, expected):
    from routes.api_v1 import project_brain as routes
    from lib.project_dispatch.contract import query_parameters
    principal = PrincipalContext.user(subject_id='user:7', owner_user_id=7, scopes={'chat'})
    calls = []
    def read_only(**kwargs):
        calls.append(kwargs)
        return {'assignments': [], 'olderCursor': None, 'newerCursor': None}
    monkeypatch.setattr(routes, '_dispatch_service', lambda path: (SimpleNamespace(history_page=read_only), principal))
    monkeypatch.setattr(routes, 'api_ok', lambda value: value)
    handler = routes.project_assignment_list
    assert handler._api_meta['parameters'] == query_parameters()
    async def run():
        app = Quart(__name__)
        async with app.test_request_context('/?path=/project' + suffix):
            return inspect.unwrap(handler)()
    assert asyncio.run(run()) == {'assignments': [], 'olderCursor': None, 'newerCursor': None}
    assert calls == [{'principal': principal, 'command_id': None, 'assignment_id': None, 'cursor': None, **expected}]


@pytest.mark.parametrize('suffix', ['commandId=', 'commandId=x&cursor=y', 'assignmentId=x&cursor=y', 'assignmentId=x&commandId=y'])
def test_empty_command_query_is_denied_before_service_lookup(monkeypatch, suffix):
    from routes.api_v1 import project_brain as routes
    monkeypatch.setattr(routes, '_dispatch_service', lambda *a: pytest.fail('Invalid query reached service'))
    monkeypatch.setattr(routes, '_safe_route_error_response', lambda error: ('invalid', 400))
    async def run():
        app = Quart(__name__)
        async with app.test_request_context('/?path=/project&' + suffix):
            return inspect.unwrap(routes.project_assignment_list)()
    assert asyncio.run(run()) == ('invalid', 400)


@pytest.mark.parametrize('authenticated', [True, False])
def test_v4_asgi_get_preserves_auth_scope_and_read_only_command_projection(monkeypatch, authenticated):
    from routes.api_v1 import project_brain as routes, auth
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    principal = PrincipalContext.user(subject_id='user:7', owner_user_id=7, scopes={'chat'})
    calls = []
    empty = {'assignments': [], 'olderCursor': None, 'newerCursor': None}
    service = SimpleNamespace(history_page=lambda **kwargs: calls.append(kwargs) or empty)
    monkeypatch.setattr(routes, '_dispatch_service', lambda path: (service, principal))
    monkeypatch.setattr(auth, 'current_auth', lambda: SimpleNamespace(
        is_authenticated=authenticated, has_scope=lambda scope: scope == 'chat'))
    app = Quart(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(
        routes.api_v1_project_brain_bp, name='offline_dispatch_v4')))
    async def run():
        async with app.test_client() as client:
            return await client.get('/api/v4/project/coordination/assignments?path=/project&commandId=exact')
    response = asyncio.run(run())
    assert response.status_code == (200 if authenticated else 401)
    if authenticated:
        assert asyncio.run(response.get_json())['data'] == empty
        assert calls == [{'principal': principal, 'command_id': 'exact', 'assignment_id': None, 'cursor': None}]
    else:
        assert calls == []


@pytest.mark.parametrize('mutation', [
    lambda refs: refs.extend([deepcopy(refs[0])] * 8),
    lambda refs: refs[0].update(resultRevision=0),
    lambda refs: refs[0].update(grantRevision=True),
    lambda refs: refs[0].update(checkpointSha=''),
    lambda refs: refs[0].update(unexpected=True),
])
def test_dependency_request_contract_rejects_unbounded_or_ambiguous_references(mutation):
    refs = [{'assignmentId': 'earlier-assignment', 'grantRevision': 1, 'resultRevision': 3}]
    validator = Draft202012Validator(schema('dependencies'))
    assert validator.is_valid(refs)
    mutation(refs)
    assert not validator.is_valid(refs)


def test_dependency_contract_preserves_exact_checkpoint_and_has_strict_states():
    reference = {'assignmentId': 'earlier-file-result', 'grantRevision': 2,
                 'resultRevision': 11, 'checkpointSha': 'exact-checkpoint'}
    assert Draft202012Validator(schema('dependency')).is_valid(reference)
    for status, reason in [('ready', 'ready'), ('waiting_review', 'review_required'),
                           ('waiting_delivery', 'delivery_required'), ('invalid', 'result_changed'),
                           ('unknown', 'evidence_unavailable')]:
        state = {'reference': deepcopy(reference), 'status': status, 'reason': reason}
        assert Draft202012Validator(schema('dependencyState')).is_valid(state)
        state['executorStarted'] = True
        assert not Draft202012Validator(schema('dependencyState')).is_valid(state)


def test_create_route_passes_dependency_pins_without_inference(monkeypatch):
    from routes.api_v1 import project_brain as routes
    from lib.project_dispatch import contract
    principal = PrincipalContext.user(subject_id='user:7', owner_user_id=7,
                                      tenant_id='tenant-a', scopes={'chat'})
    body = {'projectPath': '/project', 'coordinatorId': 'coordinator', 'recipientId': 'recipient',
            'authorizationSource': {'conversationId': 'coordinator', 'turnId': 'source', 'projectionRevision': 1},
            'brief': 'Depend on reviewed file result', 'model': 'model-a',
            'budget': {'maxTokens': 100, 'maxModelRequests': 1, 'deadline': 2000},
            'dependencies': [{'assignmentId': 'earlier', 'grantRevision': 2,
                              'resultRevision': 11, 'checkpointSha': 'pinned-sha'}]}
    calls = []
    def authorize(request, **kwargs):
        calls.append((deepcopy(request), kwargs))
        return {'assignmentId': 'dependent', 'dispatch': 'deferred', 'canonicalDelivery': False,
                'dependencyStates': [{'reference': request['dependencies'][0],
                                      'status': 'waiting_delivery', 'reason': 'delivery_required'}]}
    monkeypatch.setattr(routes, '_dispatch_service', lambda path: (
        SimpleNamespace(authorize_and_dispatch=authorize), principal))
    monkeypatch.setattr(routes, 'api_ok', lambda value: value)
    monkeypatch.setattr(contract, 'parse', lambda definition: deepcopy(body))
    assert Draft202012Validator(schema('request')).is_valid(body)
    async def run():
        app = Quart(__name__)
        async with app.test_request_context('/', headers={'Idempotency-Key': 'stable-dependent'}):
            return inspect.unwrap(routes.project_assignment_create)()
    result = asyncio.run(run())
    assert calls == [(body, {'principal': principal, 'command_id': 'stable-dependent'})]
    assert result['dispatch'] == 'deferred'
    assert result['dependencyStates'][0]['reference'] == body['dependencies'][0]
