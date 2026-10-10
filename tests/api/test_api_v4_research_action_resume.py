"""Authenticated resume delegates an unchanged task identity/policy to one service."""
import asyncio
from quart import Quart,g,request
import pytest
from lib.api_keys import local_admin_context
from lib.api_v4 import attach_v4_envelope,copy_blueprint_to_v4
from lib.storage.errors import StorageError

pytestmark=pytest.mark.unit


def test_resume_owned_empty_body_and_typed_failures(monkeypatch):
    from routes.api_v1.research import api_v1_research_bp
    calls=[]
    def resume(task_id,*,user_id):
        calls.append((task_id,user_id))
        if task_id=='missing':raise LookupError()
        if task_id=='conflict':raise StorageError('database_conflict','original frontier changed')
        return {'task_id':task_id,'resumed':True,'deduped':False}
    monkeypatch.setattr('lib.research.action_continuation.resume_action',resume)
    app=Quart(__name__);app.config['TESTING']=True
    @app.before_request
    async def grant():
        context=local_admin_context();context.owner_user_id=int(request.headers.get('X-Test-User','1'))
        g.auth_ctx=context;g.rate_decision=None
    app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(api_v1_research_bp,name='action_resume_v4')))
    async def exercise():
        client=app.test_client()
        response=await client.post('/api/v4/research/actions/original/resume',json={})
        assert response.status_code==200
        assert (await response.get_json())['data']['task_id']=='original'
        response=await client.post('/api/v4/research/actions/original/resume',json={},headers={'X-Test-User':'2'})
        assert response.status_code==200 and calls[-1]==('original',2)
        before=len(calls)
        for replacement in ({'model':'other'},{'logical_budget':{'dispatch_budget':100}},{'confirm_external_writes':True}):
            assert (await client.post('/api/v4/research/actions/original/resume',json=replacement)).status_code==400
        assert len(calls)==before
        assert (await client.post('/api/v4/research/actions/missing/resume',json={})).status_code==404
        assert (await client.post('/api/v4/research/actions/conflict/resume',json={})).status_code==409
    asyncio.run(exercise())


def test_unfinished_discovery_passes_only_authenticated_workspace_scope(monkeypatch):
    from routes.api_v1.research import api_v1_research_bp
    calls=[]
    def discover(direction,*,lang,user_id):
        calls.append((direction,lang,user_id))
        if not direction:raise ValueError('direction required')
        return {'tasks':[],'complete':True}
    monkeypatch.setattr('lib.research.action_discovery.discover_unfinished_actions',discover)
    app=Quart(__name__);app.config['TESTING']=True
    @app.before_request
    async def grant():
        context=local_admin_context();context.owner_user_id=int(request.headers.get('X-Test-User','1'))
        g.auth_ctx=context;g.rate_decision=None
    app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(api_v1_research_bp,name='unfinished_v4')))
    async def exercise():
        client=app.test_client()
        response=await client.get('/api/v4/research/actions/unfinished?direction=Original&lang=zh',headers={'X-Test-User':'2'})
        assert response.status_code==200 and calls[-1]==('Original','zh',2)
        assert (await response.get_json())['data']=={'tasks':[],'complete':True}
        assert (await client.get('/api/v4/research/actions/unfinished')).status_code==400
    asyncio.run(exercise())
