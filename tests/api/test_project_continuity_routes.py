"""Real Quart v4 continuity adapters; no paid execution or grant writes."""
import asyncio
from copy import deepcopy
from importlib import import_module
from inspect import signature
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from quart import Quart, g

from lib.identity import PrincipalContext
from lib.project_dispatch import continuity_contract as contract
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit
BASE = '/api/v4/project/coordination'
SOURCE = {'conversationId': 'coordinator', 'turnId': 'human-turn', 'projectionRevision': 17}
REFERENCE = {'id': 'declared-file', 'revision': 11, 'digest': 'a' * 64}
BODIES = {
    ('scope_changes', 'request'): {'projectPath': '/project', 'coordinatorId': 'coordinator',
        'authorizationSource': SOURCE, 'kind': 'shrink', 'note': 'Withdraw exact scope',
        'targets': [{'assignmentId': 'assignment', 'grantRevision': 23}]},
    ('scope_changes', 'retry'): {'projectPath': '/project', 'changeId': 'same-change'},
    ('responsibilities', 'create'): {'projectPath': '/project', 'name': 'Bounded responsibility',
        'assignment': {'projectPath': '/project', 'coordinatorId': 'coordinator', 'recipientId': 'recipient',
            'authorizationSource': SOURCE, 'brief': 'Consult only', 'model': 'synthetic-model',
            'budget': {'maxTokens': 100, 'maxModelRequests': 1, 'deadline': 2000},
            'profile': {'mode': 'consult', 'tools': [], 'readPaths': [], 'writePaths': [], 'maxToolCalls': 0}},
        'trigger': {'kind': 'manual'}, 'allowance': {'maxRuns': 2, 'maxTotalTokens': 200, 'deadline': 2000},
        'perRunSeconds': 60},
    ('shared_files', 'register'): {'projectPath': '/project', 'path': 'notes.md',
        'purpose': 'Explicit context', 'expectedRevision': 13, 'authorizationSource': SOURCE},
    ('shared_files', 'read'): {'projectPath': '/project', 'reference': REFERENCE, 'maxBytes': 512},
    ('shared_files', 'refresh'): {'projectPath': '/project', 'reference': REFERENCE,
        'replacementPath': 'replacement.md', 'authorizationSource': SOURCE},
}
for action in ('pause', 'cancel', 'reconcile'):
    BODIES['responsibilities', action] = {
        'projectPath': '/project', 'responsibilityId': 'responsibility', 'expectedRevision': 19}

# domain, definition, suffix, handler, application method, idempotency port
POSTS = [
    ('scope_changes', 'request', '/scope-changes', 'project_scope_change_create', 'create', True),
    ('scope_changes', 'retry', '/scope-changes/retry', 'project_scope_change_retry', 'retry', False),
    ('responsibilities', 'create', '/responsibilities', 'project_responsibility_create', 'create', True),
    *[('responsibilities', a, '/responsibilities/' + a, 'project_responsibility_' + a, a, True)
      for a in ('pause', 'cancel', 'reconcile')],
    ('shared_files', 'register', '/shared-files/register', 'project_shared_file_register', 'register', True),
    ('shared_files', 'read', '/shared-files/read', 'project_shared_file_read', 'read', False),
    ('shared_files', 'refresh', '/shared-files/refresh', 'project_shared_file_refresh', 'refresh', False),
]
GETS = [
    ('scope_changes', 'listQuery', '/scope-changes', 'project_scope_change_list',
     {'path': '/project', 'commandId': 'same-command'}, {'command_id': 'same-command'}, 'scopeChanges'),
    ('responsibilities', 'listQuery', '/responsibilities', 'project_responsibility_list',
     {'path': '/project'}, {}, 'responsibilities'),
    ('responsibilities', 'readQuery', '/responsibilities/read', 'project_responsibility_read',
     {'path': '/project', 'responsibilityId': 'responsibility'}, {}, None),
    ('shared_files', 'listQuery', '/shared-files', 'project_shared_files_list',
     {'path': '/project', 'cursor': 'exact-cursor'}, {'cursor': 'exact-cursor'}, None),
]


@pytest.fixture
def http(monkeypatch):
    from routes.api_v1 import project_brain as routes, auth
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    principal = PrincipalContext.user(subject_id='synthetic-owner', owner_user_id=7,
                                      tenant_id='tenant-a', scopes={'chat'})
    state = SimpleNamespace(authenticated=True, scopes={'chat'}, calls=[], constructors=[],
                            failure=None, result={'uncertain': True}, allowed=True, rates=[])
    monkeypatch.setattr(auth, 'current_auth', lambda: SimpleNamespace(
        is_authenticated=state.authenticated, has_scope=lambda scope: scope in state.scopes,
        key_id='synthetic-key', name='synthetic', scopes=state.scopes))
    monkeypatch.setattr(auth, 'audit_log', lambda *a, **kw: None)
    def delegate(domain, action, actual_signature):
        def call(*args, **kwargs):
            actual_signature.bind(None, *args, **kwargs)
            state.calls.append((domain, action, deepcopy(args), kwargs))
            if state.failure is not None:
                raise state.failure
            return deepcopy(state.result)
        return call
    for domain, class_name in [('scope_changes', 'ProjectScopeChanges'),
                               ('responsibilities', 'ProjectResponsibilities'), ('shared_files', 'ProjectSharedFiles')]:
        module = import_module('lib.project_dispatch.' + domain)
        actual_class = getattr(module, class_name)
        methods = {a: signature(getattr(actual_class, a)) for a in (
            'create', 'retry', 'list', 'read', 'pause', 'cancel', 'reconcile', 'register', 'refresh')
            if hasattr(actual_class, a)}
        constructor = signature(actual_class)
        def factory(*args, _domain=domain, _methods=methods, _constructor=constructor, **kwargs):
            _constructor.bind(*args, **kwargs)
            state.constructors.append((_domain, args, kwargs))
            return SimpleNamespace(**{a: delegate(_domain, a, sig) for a, sig in _methods.items()})
        monkeypatch.setattr(module, class_name, factory)
    from lib.project_dispatch import repository
    monkeypatch.setattr(repository, 'ProjectDispatchRepository',
                        lambda owner, path: SimpleNamespace(owner=owner, project=path))
    app = Quart(__name__)
    app.config['TESTING'] = True
    @app.before_request
    def actor():
        g.principal_context = principal
    app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(
        routes.api_v1_project_brain_bp, name='continuity_test_v4')))
    async def exchange(method, suffix, **kwargs):
        async with app.test_client() as client:
            response = await client.open(BASE + suffix, method=method, **kwargs)
            return response.status_code, await response.get_json()
    return SimpleNamespace(state=state, principal=principal, routes=routes, app=app,
                           request=lambda method, suffix, **kw: asyncio.run(exchange(method, suffix, **kw)))


@pytest.mark.parametrize('domain,definition,suffix,view,action,key', POSTS)
def test_v4_posts_derive_schema_and_delegate_exact_principal_body_revision(http, domain, definition, suffix, view, action, key):
    body = deepcopy(BODIES[domain, definition])
    handler = getattr(http.routes, view)
    assert handler._api_meta['scope'] == 'chat'
    declared = handler._api_meta['request_body']['content']['application/json']['schema']
    assert declared == contract.schema(domain, definition)
    Draft202012Validator.check_schema(declared)
    assert Draft202012Validator(declared).is_valid(body) and '$ref' not in str(declared)
    status, payload = http.request('POST', suffix, json=body, headers={'Idempotency-Key': 'same-command'})
    assert status == 200 and payload['data'] == {'uncertain': True}
    expected = {'principal': http.principal, **({'command_id': 'same-command'} if key else {})}
    args = (body['changeId'],) if action == 'retry' else (body,)
    assert http.state.calls == [(domain, action, args, expected)]
    assert http.state.calls[0][3]['principal'] is http.principal
    constructor_domain, constructor_args, constructor_kwargs = http.state.constructors[0]
    assert constructor_domain == domain and constructor_kwargs == {}
    if domain == 'scope_changes':
        assert constructor_args[0].owner == 7 and constructor_args[0].project == '/project'
    else:
        assert constructor_args == (7, '/project')


@pytest.mark.parametrize('domain,definition,suffix,view,query,extra,envelope', GETS)
def test_v4_gets_schema_query_and_side_effect_free_exact_read(http, domain, definition, suffix, view, query, extra, envelope):
    handler = getattr(http.routes, view)
    assert handler._api_meta['scope'] == 'chat'
    params, schema = handler._api_meta['parameters'], contract.schema(domain, definition)
    assert {p['name']: p['schema'] for p in params} == schema['properties']
    assert {p['name'] for p in params if p['required']} == set(schema['required'])
    http.state.result = [{'state': 'uncertain'}] if envelope else {'state': 'uncertain'}
    for _ in range(2):
        status, payload = http.request('GET', suffix, query_string=query)
        assert status == 200
        assert payload['data'] == ({envelope: http.state.result} if envelope else http.state.result)
    action = 'read' if definition == 'readQuery' else 'list'
    args = ('responsibility',) if action == 'read' else ()
    assert http.state.calls == [(domain, action, args, {'principal': http.principal, **extra})] * 2
    assert http.state.rates == []


@pytest.mark.parametrize('authenticated,scopes,expected', [(False, {'chat'}, 401), (True, {'conversations'}, 403)])
@pytest.mark.parametrize('method,suffix', [('POST', p[2]) for p in POSTS] + [('GET', q[2]) for q in GETS])
def test_v4_auth_denied_before_dto_or_application(http, authenticated, scopes, expected, method, suffix):
    http.state.authenticated, http.state.scopes = authenticated, scopes
    status, _ = http.request(method, suffix, json={})
    assert status == expected and http.state.calls == http.state.constructors == http.state.rates == []


@pytest.mark.parametrize('domain,definition,suffix,view,action,key', POSTS)
@pytest.mark.parametrize('invalid', ['unknown', 'missing_project'])
def test_v4_body_unknown_fields_and_explicit_project_required(http, domain, definition, suffix, view, action, key, invalid):
    body = deepcopy(BODIES[domain, definition])
    if invalid == 'unknown':
        body['principal'] = {'kind': 'user', 'owner_user_id': 99}
    else:
        body.pop('projectPath')
    status, _ = http.request('POST', suffix, json=body)
    assert status == 400 and http.state.calls == http.state.constructors == []


@pytest.mark.parametrize('domain,definition,suffix,mutation', [
    ('scope_changes', 'request', '/scope-changes', lambda b: b['targets'][0].update(grantRevision=True)),
    ('scope_changes', 'request', '/scope-changes', lambda b: b['authorizationSource'].update(actor='human')),
    ('responsibilities', 'create', '/responsibilities', lambda b: b['assignment'].update(principal='human')),
    ('responsibilities', 'create', '/responsibilities', lambda b: b['allowance'].update(maxRuns=True)),
    ('responsibilities', 'pause', '/responsibilities/pause', lambda b: b.update(expectedRevision=True)),
    ('shared_files', 'register', '/shared-files/register', lambda b: b.update(expectedRevision='13')),
    ('shared_files', 'read', '/shared-files/read', lambda b: b['reference'].update(revision=True)),
    ('shared_files', 'refresh', '/shared-files/refresh', lambda b: b['reference'].update(unknown=True)),
])
def test_v4_nested_dto_and_typed_revision_are_strict(http, domain, definition, suffix, mutation):
    body = deepcopy(BODIES[domain, definition])
    mutation(body)
    status, _ = http.request('POST', suffix, json=body)
    assert status == 400 and http.state.calls == http.state.constructors == []


@pytest.mark.parametrize('suffix,query', [
    ('/scope-changes', 'path=/project&commandId='),
    ('/scope-changes', 'path=/project&commandId=first&commandId=second'),
    ('/responsibilities', 'path=/project&occurrenceId=now'),
    ('/responsibilities/read', 'path=/project'),
    ('/shared-files', 'path=/project&cursor='),
    ('/shared-files', 'path=/project&path=/other'), ('/shared-files', ''),
])
def test_v4_query_rejects_unknown_duplicate_missing_or_empty_fields(http, suffix, query):
    status, _ = http.request('GET', suffix + '?' + query)
    assert status == 400 and http.state.calls == http.state.constructors == []


@pytest.mark.parametrize('failure,expected', [
    (PermissionError('private owner context'), 403), (ValueError('Invalid current revision'), 400),
    (StorageError('database_conflict', 'Current revision changed'), 409),
    (StorageError('database_unavailable', 'Unavailable', retryable=True), 503),
    (StorageError('database_timeout', 'Outcome uncertain', retryable=True), 503),
])
@pytest.mark.parametrize('method,suffix,body', [
    ('POST', '/scope-changes', BODIES['scope_changes', 'request']),
    ('POST', '/responsibilities/pause', BODIES['responsibilities', 'pause']),
    ('POST', '/shared-files/refresh', BODIES['shared_files', 'refresh']),
    ('GET', '/shared-files?path=/project', None),
])
def test_v4_typed_domain_and_storage_failure_never_becomes_success(http, failure, expected, method, suffix, body):
    http.state.failure = failure
    status, payload = http.request(method, suffix, **({'json': body} if body else {}))
    assert status == expected and payload.get('ok') is not True and len(http.state.calls) == 1


@pytest.mark.parametrize('suffix,body', [(p[2], BODIES[p[0], p[1]]) for p in POSTS if p[4] != 'read'])
def test_v4_mutations_do_not_add_fixed_request_throttles(http, suffix, body):
    # Resource and execution-grant bounds belong to the domain, not a fixed
    # single-user HTTP throughput cap. Auth and payload failures remain fenced.
    for _ in range(31):
        status, _ = http.request('POST', suffix, json=body)
        assert status == 200
    assert len(http.state.calls) == len(http.state.constructors) == 31
    assert http.state.rates == []


def test_v4_same_command_uncertain_post_read_does_not_retry_or_create(http):
    http.state.failure = StorageError('database_timeout', 'Outcome uncertain', retryable=True)
    status, _ = http.request('POST', '/scope-changes', json=BODIES['scope_changes', 'request'],
                             headers={'Idempotency-Key': 'same-command'})
    assert status == 503
    http.state.failure = None
    http.state.result = [{'changeId': 'same-change', 'state': 'uncertain'}]
    for _ in range(2):
        status, payload = http.request('GET', '/scope-changes?path=/project&commandId=same-command')
        assert status == 200 and payload['data']['scopeChanges'] == http.state.result
    assert [call[1] for call in http.state.calls] == ['create', 'list', 'list']
    assert http.state.calls[0][3]['command_id'] == http.state.calls[1][3]['command_id'] == 'same-command'


def test_v4_has_no_public_responsibility_occurrence_trigger(http):
    status, _ = http.request('POST', '/responsibilities/occurrence', json={'projectPath': '/project'})
    assert status == 404 and http.state.calls == http.state.constructors == []


@pytest.mark.parametrize('data', ['{"projectPath":', '[]', '"not an object"'])
def test_v4_invalid_json_never_delegates_or_infers_authority(http, data):
    status, _ = http.request('POST', '/scope-changes', data=data,
                             headers={'Content-Type': 'application/json'})
    assert status == 400 and http.state.calls == http.state.constructors == []


def test_v4_shared_file_read_is_side_effect_free_even_with_mutation_limiter_denied(http):
    http.state.allowed = False
    status, _ = http.request('POST', '/shared-files/read', json=BODIES['shared_files', 'read'],
                             headers={'Idempotency-Key': 'ignored-read-key'})
    assert status == 200 and http.state.rates == []
    assert http.state.calls == [('shared_files', 'read', (BODIES['shared_files', 'read'],),
                                {'principal': http.principal})]


def test_v4_body_limit_keeps_http_413_not_internal_error(http):
    http.app.config['MAX_CONTENT_LENGTH'] = 40
    status, _ = http.request('POST', '/scope-changes', json=BODIES['scope_changes', 'request'])
    assert status == 413 and http.state.calls == http.state.constructors == []
