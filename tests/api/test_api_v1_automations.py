"""Native-primary v4 delivery over real storage, shared auth and error envelopes."""
import asyncio
import pytest
from quart import Quart, g, request

from lib.api_keys import AuthContext
from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
from lib.automations.management import AutomationManagement
from lib.automations.management_contract import limits, validate
from lib.identity import PrincipalContext
from tests.support.automation_fixtures import clock_definition

pytestmark = pytest.mark.api
pytest_plugins = ('tests.support.semantic_authorities',)
BASE = '/api/v4/automations'
SCOPES = 'automations:read,automations:manage,automations:execute'


def test_http_owned_source_secrets_scopes_and_exact_replay(app, tmp_path, monkeypatch):
    from lib import credentials_vault as vault
    monkeypatch.setattr(vault, '_STORE_PATH', tmp_path / 'source-vault.json')
    monkeypatch.setattr(vault, '_KEY_PATH', tmp_path / '.source-vault.key')
    monkeypatch.setattr(vault, '_fernet', None)
    from routes.api_v1.credentials import api_v1_credentials_bp
    application, headers = app
    application.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(
        api_v1_credentials_bp,name='source_credentials_v4_fixture')))
    monkeypatch.setattr('routes.api_v1.credentials._bootstrap',lambda:None)
    headers = {**headers, 'X-Scopes': SCOPES + ',skills:install'}
    token = 'http-private-provider-fixture-0123456789'
    command = {'expectedRevision': 0, 'requestId': 'configure-http',
               'environment': 'prod', 'active': True, 'credential': token}

    async def exercise():
        client = application.test_client()
        path = BASE + '/skill-source'
        assert (await client.get(path)).status_code == 401
        denied = await client.put(path, headers={**headers, 'X-Scopes': SCOPES}, json=command)
        assert denied.status_code == 403 and vault.list_entries() == []
        response = await client.put(path, headers=headers, json=command)
        assert response.status_code == 200
        first = (await response.get_json())['data']
        validate('SkillSource', first)
        assert first['credentialConfigured'] is True
        assert token not in await response.get_data(as_text=True)
        repeated = await client.put(path, headers=headers, json=command)
        assert (await repeated.get_json())['data'] == first
        assert len(vault.list_entries()) == 1
        managed = vault.list_entries()[0]['name']
        foreign_headers = {**headers,'X-Tenant':'foreign','X-Owner':str(int(headers['X-Owner'])+1)}
        visible = await client.get('/api/v4/credentials',headers=foreign_headers)
        assert (await visible.get_json())['data']['credentials'] == []
        for name in (managed,managed.upper()):
            reveal = await client.post('/api/v4/credentials/'+name+'/reveal',headers=foreign_headers)
            assert reveal.status_code == 404 and token not in await reveal.get_data(as_text=True)
            remove = await client.delete('/api/v4/credentials/'+name,headers=foreign_headers)
            assert remove.status_code == 404
            overwrite = await client.post('/api/v4/credentials',headers=foreign_headers,
                json={'name':name,'value':'forged','note':'forged'})
            assert overwrite.status_code == 403
        assert vault.get_entry(managed) == token

        foreign = await client.get(path, headers={**headers, 'X-Tenant': 'foreign'})
        assert (await foreign.get_json())['data']['revision'] == 0
        rejected = await client.put(path, headers=headers, json={**command, 'credential': token + '\n'})
        assert rejected.status_code == 400
        assert token not in await rejected.get_data(as_text=True)
        assert len(vault.list_entries()) == 1
        stale = await client.put(path, headers=headers, json={**command, 'requestId': 'stale', 'credential': token + '-other'})
        assert stale.status_code == 409 and len(vault.list_entries()) == 1
    asyncio.run(exercise())


@pytest.fixture
def app(semantic_authority, monkeypatch):
    from routes.api_v1 import automations
    from lib.http_transport.error_handlers import register_http_error_handlers
    factory, owner, _ = semantic_authority
    application = Quart(__name__, static_folder=None)
    application.config['TESTING'] = True

    @application.before_request
    def fixture_auth():
        if request.headers.get('X-Owner'):
            owner_id = int(request.headers['X-Owner'])
            scopes = frozenset(request.headers.get('X-Scopes', SCOPES).split(','))
            g.auth_ctx = AuthContext(key_id='fixture', owner_user_id=owner_id, scopes=scopes)
            g.principal_context = PrincipalContext.user(subject_id='http-fixture',
                owner_user_id=owner_id, tenant_id=request.headers.get('X-Tenant', 'http-tests'), scopes=scopes)

    application.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(
        automations.api_v1_automations_bp, name='automations_v4_fixture')))
    register_http_error_handlers(application)
    monkeypatch.setattr(automations, 'service_for', lambda principal:
                        AutomationManagement(principal, client_factory=factory))
    return application, {'X-Owner': str(owner)}


async def publish(client, headers):
    rule = clock_definition()
    response = await client.put(BASE+'/definitions/'+rule['id'], headers=headers,
        json={'expectedRevision': 0, 'requestId': 'publish-http', 'definition': rule})
    assert response.status_code == 200
    result = (await response.get_json())['data']
    validate('Definition', result)
    return result


def test_http_shared_auth_validation_and_owner_isolation(app):
    application, headers = app

    async def exercise():
        client = application.test_client()
        assert (await client.get(BASE+'/actions')).status_code == 401
        assert (await client.get(BASE+'/actions', headers={**headers, 'X-Scopes': 'tasks'})).status_code == 403
        definition = await publish(client, headers)
        path = BASE+'/definitions/'+definition['id']
        for foreign in ({**headers, 'X-Owner': str(int(headers['X-Owner'])+1)},
                        {**headers, 'X-Tenant': 'other-tenant'}):
            response = await client.get(path, headers=foreign)
            assert response.status_code == 404
            problem = await response.get_json()
            assert problem['code'] == 'automation_not_found'
            assert response.content_type == 'application/problem+json'
        for mutation in ({'ownerId': 99}, {'expectedRevision': True}, {'requestId': ''}, {'trigger': {}}):
            response = await client.post(path+'/runs', headers=headers,
                json={'expectedRevision': 1, 'requestId': 'manual-http', **mutation})
            assert response.status_code == 400
        response = await client.post(path+'/runs', headers={**headers, 'Content-Type': 'application/json'}, data=b'{')
        assert response.status_code == 400 and response.content_type == 'application/problem+json'
        denied = await client.post(path+'/pause', headers={**headers, 'X-Scopes': 'automations:read'},
                                  json={'expectedRevision': 1, 'requestId': 'denied'})
        assert denied.status_code == 403
        nested = clock_definition()
        condition = True
        for _ in range(100):
            condition = {'not': condition}
        nested['steps'][0]['when'] = condition
        response = await client.put(path, headers=headers, json={
            'expectedRevision': 1, 'requestId': 'too-deep', 'definition': nested})
        assert response.status_code == 400
        assert (await response.get_json())['code'] == 'automation_document_too_complex'
    asyncio.run(exercise())


def test_http_publication_manual_history_and_cancellation(app):
    application, headers = app

    async def exercise():
        client = application.test_client()
        definition = await publish(client, headers)
        path = BASE+'/definitions/'+definition['id']
        command = {'expectedRevision': 1, 'requestId': 'manual-http'}
        first = await client.post(path+'/runs', headers=headers, json=command)
        assert first.status_code == 200
        run = (await first.get_json())['data']; validate('Run', run)
        repeated = await client.post(path+'/runs', headers=headers, json=command)
        assert (await repeated.get_json())['data'] == run
        assert run['trigger']['origin'] == 'human'
        response = await client.post(path+'/pause', headers=headers,
                                     json={'expectedRevision': 1, 'requestId': 'pause-http'})
        assert (await response.get_json())['data']['status'] == 'paused'
        conflict = await client.post(path+'/pause', headers=headers,
                                    json={'expectedRevision': 1, 'requestId': 'different'})
        assert conflict.status_code == 409
        assert (await conflict.get_json())['code'] == 'automation_conflict'
        cancel = await client.post(BASE+'/runs/'+run['id']+'/cancel', headers=headers,
                                   json={'requestId': 'cancel-http', 'reason': 'Requested by owner'})
        assert cancel.status_code == 200
        cancelled = (await cancel.get_json())['data']; validate('Run', cancelled)
        assert cancelled['status'] == 'cancelled'
        response = await client.get(path+'/runs?limit=1', headers=headers)
        history = (await response.get_json())['data']; validate('RunPage', history)
        assert history['items'] == [cancelled]
        response = await client.get(path+'/runs', headers=headers,
                                    query_string={'before': history['nextCursor'], 'limit': '1'})
        assert (await response.get_json())['data']['items'] == []
        for query in ('limit=9', 'limit=1&limit=2', 'limit=true', 'before=../', 'ownerId=1'):
            assert (await client.get(path+'/runs?'+query, headers=headers)).status_code == 400
    asyncio.run(exercise())


def test_http_body_bound_and_catalog_has_no_storage_side_effect(app, monkeypatch):
    application, headers = app
    from routes.api_v1 import automations

    class ReadOnlyCatalogueClient:
        def query(self, *args, **kwargs):
            raise AssertionError('Catalogue discovery opened storage')
        command = query
    monkeypatch.setattr(automations, 'service_for', lambda principal: AutomationManagement(
        principal, client_factory=lambda **_: ReadOnlyCatalogueClient()))

    async def exercise():
        client = application.test_client()
        response = await client.get(BASE+'/actions', headers=headers)
        assert response.status_code == 200
        validate('ActionPage', (await response.get_json())['data'])
        response = await client.post(BASE+'/definitions/example/runs',
            headers={**headers, 'Content-Type': 'application/json'}, data=b' '*(limits()['requestBytes']+1))
        assert response.status_code == 413
        assert response.content_type == 'application/problem+json'
    asyncio.run(exercise())


def test_http_owned_approval_is_scoped_bounded_and_redacts_private_proofs(app, semantic_authority):
    from tests.automations.test_atomic_approval import pending_approval
    from tests.automations.test_execution_handler import job
    application, headers = app
    factory, repo, run, runner, calls = pending_approval(semantic_authority)
    assert runner.run_once().state == 'suspended'
    waiting = repo.get('run', run.id)
    headers = {**headers, 'X-Tenant':repo.principal.tenant_id,'X-Scopes':SCOPES+',skills:install'}
    body = {'stepId':'write','expectedRevision':waiting.version,'requestId':'http-approve'}
    path = BASE+'/runs/'+run.id+'/approve'
    async def exercise():
        client = application.test_client()
        assert (await client.post(path,json=body)).status_code == 401
        for scope in ('automations:manage','automations:execute','skills:install'):
            denied = {**headers,'X-Scopes':','.join(s for s in headers['X-Scopes'].split(',') if s!=scope)}
            assert (await client.post(path,headers=denied,json=body)).status_code == 403
        for other in ({**headers,'X-Owner':str(repo.owner_id+1)},
                      {**headers,'X-Tenant':'other'}):
            assert (await client.post(path,headers=other,json=body)).status_code == 404
        for change in ({'ownerId':repo.owner_id},{'actor_principal':{}},
                       {'arguments':{}},{'stepId':'../write'},{'expectedRevision':True}):
            assert (await client.post(path,headers=headers,json={**body,**change})).status_code == 400
        response = await client.post(path,headers=headers,json=body)
        assert response.status_code == 200
        accepted = (await response.get_json())['data']
        validate('Run',accepted)
        assert set(accepted['steps']['write']['approval']) == {'requestId','approvedAt'}
        assert (await (await client.post(path,headers=headers,json=body)).get_json())['data'] == accepted
        assert (await client.post(path,headers=headers,json={**body,'requestId':'different'})).status_code == 409
    asyncio.run(exercise())
    assert calls == [] and job(factory,repo,run)['wakeSequence']==1
    assert runner.run_once().state == 'succeeded'


def test_http_task_presentations_auth_and_missing_target(app):
    application,headers=app
    async def exercise():
        client=application.test_client()
        path=BASE+'/conversations/missing/attempts/missing/presentations'
        assert (await client.get(path)).status_code==401
        assert (await client.get(path,headers={**headers,'X-Scopes':'tasks'})).status_code==403
        assert (await client.get(path,headers=headers)).status_code==404
        assert (await client.get(path+'?limit=9',headers=headers)).status_code==400
        assert (await client.get(path+'?after=bad',headers=headers)).status_code==400
    asyncio.run(exercise())


def test_http_owned_project_registration_auth_validation_and_scope(app,tmp_path):
    application,headers=app
    async def exercise():
        client=application.test_client();path=BASE+'/projects'
        body={'root':str(tmp_path),'label':'Owned project','active':True,'expectedRevision':0,'requestId':'register'}
        assert (await client.put(path,json=body)).status_code==401
        assert (await client.put(path,json=body,headers={**headers,'X-Scopes':'automations:read'})).status_code==403
        assert (await client.put(path,json={**body,'ownerId':123},headers=headers)).status_code==400
        assert (await client.put(path,json={**body,'root':'relative'},headers=headers)).status_code==400
        response=await client.put(path,json=body,headers=headers)
        assert response.status_code==200
        project=(await response.get_json())['data'];validate('Project',project)
        assert set(project)=={'id','revision','root','label','active','createdAt','updatedAt'}
        assert (await client.put(path,json=body,headers=headers)).status_code==200
        assert (await client.put(path,json={**body,'label':'conflicting'},headers=headers)).status_code==409
        assert (await client.get(path+'/'+project['id'],headers={**headers,'X-Tenant':'foreign'})).status_code==404
        assert (await client.get(path+'?limit=9',headers=headers)).status_code==400
        listed=(await (await client.get(path,headers=headers)).get_json())['data']
        validate('ProjectPage',listed);assert listed['items']==[project]
        rule=clock_definition();rule['projectId']=project['id']
        response=await client.put(BASE+'/definitions/'+rule['id'],headers=headers,
            json={'expectedRevision':0,'requestId':'project-rule','definition':rule})
        assert response.status_code==200
    asyncio.run(exercise())
