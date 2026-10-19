"""Real v4 ASGI mount with controlled domain service; no backend/provider calls."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from quart import Quart
from jsonschema import Draft202012Validator

from lib.identity import PrincipalContext
from lib.project_dispatch.contract import schema

pytestmark = pytest.mark.unit


@pytest.fixture
def batch_api(monkeypatch):
    from routes.api_v1 import project_dispatch_batches as routes, auth
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    principal = PrincipalContext.user(subject_id='user:7', owner_user_id=7,
                                      tenant_id='tenant-a', scopes={'chat'})
    body = {'projectPath': '/project', 'coordinatorId': 'coordinator',
            'authorizationSource': {'conversationId': 'coordinator', 'turnId': 'human', 'projectionRevision': 1},
            'objective': 'Optimize Tofu reliability and performance.',
            'budget': {'maxTokens': 200, 'maxModelRequests': 2, 'deadline': 2000},
            'workers': [{'recipientId': 'worker-a', 'brief': 'Measure reliability.', 'model': 'model-a',
                         'budget': {'maxTokens': 100, 'maxModelRequests': 1, 'deadline': 2000}},
                        {'recipientId': 'worker-b', 'brief': 'Measure performance.', 'model': 'model-a',
                         'budget': {'maxTokens': 100, 'maxModelRequests': 1, 'deadline': 2000}}]}
    state = SimpleNamespace(calls=[], authenticated=True, allowed=True, body=body,
                            principal=principal, routes=routes)
    def record(action, *args, **kwargs):
        state.calls.append((action, deepcopy(args), kwargs))
        assert kwargs['principal'] is principal
        return None if action == 'read' else {'batchId': 'original-batch'}
    service = SimpleNamespace(
        authorize_batch_and_dispatch=lambda *a, **kw: record('create', *a, **kw),
        dispatch_batch=lambda *a, **kw: record('dispatch', *a, **kw),
        batch_receipt=lambda **kw: record('read', **kw))
    monkeypatch.setattr(routes, '_dispatch_service', lambda path: (service, principal))
    monkeypatch.setattr(auth, 'current_auth', lambda: SimpleNamespace(
        is_authenticated=state.authenticated, key_id='fixture-key', name='batch-fixture',
        scopes={'chat'} if state.allowed else set(),
        has_scope=lambda scope: state.allowed and scope == 'chat'))
    app = Quart(__name__)
    app.config['TESTING'] = True
    from lib.http_transport.error_handlers import register_http_error_handlers
    register_http_error_handlers(app)
    app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(
        routes.api_v1_project_brain_bp, name='batch_v4')))
    state.app = app
    return state


def request(state, method, suffix='', **kwargs):
    async def run():
        async with state.app.test_client() as client:
            response = await getattr(client, method)('/api/v4/project/coordination/batches' + suffix, **kwargs)
            return response.status_code, await response.get_json()
    return asyncio.run(run())


def test_batch_create_and_dispatch_pass_exact_original_authority(batch_api):
    s = batch_api
    assert Draft202012Validator(schema('batchRequest')).is_valid(s.body)
    status, value = request(s, 'post', json=s.body, headers={'Idempotency-Key': 'stable-batch'})
    assert status == 200 and value['data']['batchId'] == 'original-batch'
    assert s.calls == [('create', (s.body,), {'principal': s.principal, 'command_id': 'stable-batch'})]
    status, _ = request(s, 'post', '/dispatch', json={'projectPath': '/project', 'batchId': 'original-batch'})
    assert status == 200
    assert s.calls[-1] == ('dispatch', ('original-batch',), {'principal': s.principal})


def test_batch_receipt_is_exact_read_only_and_absence_is_null(batch_api):
    s = batch_api
    status, value = request(s, 'get', '?path=/project&commandId=original-command')
    assert status == 200 and value['data'] == {'batch': None}
    assert s.calls == [('read', (), {'principal': s.principal, 'command_id': 'original-command'})]


@pytest.mark.parametrize('query', ['?path=/project', '?path=/project&commandId=', '?commandId=command'])
def test_receipt_requires_both_bounded_exact_identifiers(batch_api, query):
    status, _ = request(batch_api, 'get', query)
    assert status == 400 and not batch_api.calls


@pytest.mark.parametrize('mutation', [
    lambda b: b.update(_principal='user'),
    lambda b: b['workers'][0].update(toolPermission='all'),
    lambda b: b['workers'][0]['budget'].update(maxTokens=True),
    lambda b: b.update(workers=[]),
    lambda b: b['authorizationSource'].update(projectionRevision=0),
])
def test_invalid_or_permission_smuggling_body_never_reaches_service(batch_api, mutation):
    body = deepcopy(batch_api.body); mutation(body)
    status, _ = request(batch_api, 'post', json=body, headers={'Idempotency-Key': 'invalid'})
    assert status == 400 and not batch_api.calls


@pytest.mark.parametrize('method,suffix', [('get', '?path=/project&commandId=command'),
                                          ('post', ''), ('post', '/dispatch')])
@pytest.mark.parametrize('mode,status', [('unauthenticated', 401), ('no-scope', 403)])
def test_all_batch_surfaces_enforce_shared_auth_and_chat_scope(batch_api, method, suffix, mode, status):
    s = batch_api
    s.authenticated = mode != 'unauthenticated'
    s.allowed = mode != 'no-scope'
    body = {'projectPath': '/project', 'batchId': 'original-batch'} if suffix == '/dispatch' else s.body
    actual, _ = request(s, method, suffix, **({'json': body} if method == 'post' else {}))
    assert actual == status and not s.calls


def test_batch_schemas_have_strict_local_authority_and_route_metadata(batch_api):
    s = batch_api
    for definition in ('batchRequest', 'batchRetry', 'batchView'):
        document = schema(definition)
        Draft202012Validator.check_schema(document)
        assert document['additionalProperties'] is False and '$ref' not in str(document)
    for name, definition in [('project_batch_create', 'batchRequest'), ('project_batch_dispatch', 'batchRetry')]:
        meta = getattr(s.routes, name)._api_meta
        assert meta['scope'] == 'chat'
        assert meta['request_body']['content']['application/json']['schema'] == schema(definition)
