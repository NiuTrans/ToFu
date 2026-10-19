"""Surface admission and ownership; provider fakes never allocate resources."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from jsonschema import Draft202012Validator, ValidationError
from lib import response_surfaces as service
from lib.response_surfaces.validation import validate_surface_semantics

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.unit


def catalog():
    return {'provider': 'test', 'queues': [{'name': 'training', 'resources': [
        {'kind': 'gpu', 'name': 'H200', 'unit': 'count', 'total': 8, 'allocated': 8, 'available': 0}]}]}


def test_snapshot_preserves_zero_and_absent_pending():
    value = service.resource_snapshot(catalog())
    assert value['rows'][0]['available'] == 0
    assert 'pending' not in value['rows'][0]
    payload = {'version': 1, 'title': 'Resources', 'blocks': [
        {'type': 'resource_panel', 'id': 'resources', 'title': 'Resources', 'provider': 'test', 'snapshot': value}]}
    Draft202012Validator(json.loads((ROOT / 'contracts/rich_response_v1.schema.json').read_text())).validate(payload)
    validate_surface_semantics(payload)
    value['rows'].append(copy.deepcopy(value['rows'][0]))
    with pytest.raises(ValueError, match='Duplicate'):
        validate_surface_semantics(payload)


def test_duplicate_provider_rows_are_refused():
    value = catalog(); value['queues'].append(copy.deepcopy(value['queues'][0]))
    with pytest.raises(ValueError, match='duplicate'):
        service.resource_snapshot(value)


def test_read_rechecks_scope_after_provider_and_never_changes_actor(monkeypatch):
    calls = []
    monkeypatch.setattr(service, 'resolve_scope', lambda scope, user_id: '/owned' if user_id == 7 else (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(service, 'read_catalog', lambda provider, **kw: calls.append(kw) or catalog())
    result = service.read_resources({'runId': 'run'}, 'test', user_id=7, tenant_id='tenant')
    assert result['snapshot']['rows'][0]['available'] == 0
    assert calls[0]['user_id'] == 7 and calls[0]['tenant_id'] == 'tenant' and calls[0]['project_path'] == '/owned'
    with pytest.raises(PermissionError):
        service.read_resources({'runId': 'run'}, 'test', user_id=8)
    assert len(calls) == 1
    values = iter(['/owned', '/changed'])
    monkeypatch.setattr(service, 'resolve_scope', lambda *a, **kw: next(values))
    with pytest.raises(PermissionError, match='changed'):
        service.read_resources({'runId': 'run'}, 'test', user_id=7)


@pytest.mark.parametrize('scope', [{}, {'runId': 'run', 'projectPath': '/foreign'}, {'runId': 'run', 'conversationId': 'chat'}, {'conversationId': ''}])
def test_model_context_cannot_select_owner_or_path(scope):
    with pytest.raises(PermissionError):
        service.resolve_scope(scope, user_id=7)


def test_layout_requires_complete_unambiguous_placement():
    payload = {'blocks': [{'id': 'one', 'type': 'notice'}], 'layout': {'groups': [{'id': 'g', 'blocks': ['one', 'one']}]}}
    with pytest.raises(ValueError, match='exactly once'):
        validate_surface_semantics(payload)


def test_headless_evolution_discovery_does_not_expose_conversation_file_writes():
    from lib.tools.rich_response import build_tools
    ctx = SimpleNamespace(durable_state_available=True, owner_user_id=7, conv_id='', cfg={'evolution': {'run_id': 'run'}})
    assert [row['function']['name'] for row in build_tools(ctx)] == ['publish_rich_response']
    ctx.cfg = {}
    assert build_tools(ctx) == []


def test_mcp_queue_read_is_exact_read_only_owner_bound_and_never_includes_apps(monkeypatch):
    from lib.response_surfaces import mcp
    name = 'mcp__capacity__get_queue_resource'
    calls = []
    enabled = [True]
    class Bridge:
        def get_tool_info(self, selected):
            assert selected == name
            return {'tool_name': 'get_queue_resource', 'input_schema': {'type': 'object'}}
        def get_tool_safety(self): return {name: enabled[0]}
        def call_tool(self, selected, arguments, **kwargs):
            calls.append((selected, arguments, kwargs))
            return json.dumps({'queue': 'root.training', 'summary': {'gpu': {'H200': {'total':8,'allocated':8,'remain':0}}}})
    bridge = Bridge()
    def tools(owner):
        assert owner == 7
        return bridge, [{'tool_name':'get_queue_resource','server_name':'capacity','namespaced_name':name}]
    monkeypatch.setattr(mcp, '_tools', tools)
    result=mcp.read_catalog('capacity','queue',user_id=7,cancelled=lambda:False,queue='root.training')
    assert result['queues'][0]['resources'][0]['available'] == 0
    assert result['queues'][0]['resources'][1]['available'] is None
    assert calls[0][1] == {'queue':'root.training','include_apps':False}
    assert calls[0][2]['retry_on_disconnect'] is False
    enabled[0]=False
    with pytest.raises(PermissionError):
        mcp.read_catalog('capacity','queue',user_id=7,cancelled=lambda:False,queue='root.training')
    assert len(calls)==1


def test_http_owner_errors_private_cache_and_no_provider_detail_leak(monkeypatch):
    import asyncio
    async def scenario():
        from quart import Quart, g
        from routes.response_assets import response_assets_bp
        app = Quart(__name__); app.register_blueprint(response_assets_bp)
        @app.before_request
        async def identity():
            g.auth_ctx = SimpleNamespace(is_authenticated=True, owner_user_id=7, key_id='test', tenant_id='tenant', scopes=[])
        observed = []
        def read(scope, provider, **kw):
            observed.append((scope, provider, kw['user_id'], kw['tenant_id']))
            return {'version': 1, 'provider': provider, 'snapshot': service.resource_snapshot(catalog())}
        monkeypatch.setattr(service, 'read_resources', read)
        client = app.test_client()
        result = await client.get('/api/response-surfaces/resources?runId=run&provider=test')
        assert result.status_code == 200 and result.headers['Cache-Control'] == 'private, no-store'
        assert observed == [({'runId': 'run'}, 'test', 7, 'tenant')]
        assert (await client.get('/api/response-surfaces/resources?runId=run&provider=test&owner=8')).status_code == 400
        monkeypatch.setattr(service, 'read_resources', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('SECRET provider credential')))
        result = await client.get('/api/response-surfaces/resources?runId=run&provider=test')
        assert result.status_code == 503 and 'SECRET' not in await result.get_data(as_text=True)
    asyncio.run(scenario())


def test_sdk_read_port_preserves_actor_and_closes_transport_on_error(monkeypatch):
    from tofu_environments import host
    from tofu_environments.contracts import Actor
    closed=[];seen=[]
    def compose(task, *, jobs, lifecycle, cancelled):
        assert task == {'_userId':7,'_tenantId':'tenant'} and jobs
        lifecycle.callback(lambda:closed.append(True))
        def catalog(actor, provider):
            seen.append((actor,provider));raise RuntimeError('offline')
        return SimpleNamespace(catalog=catalog)
    monkeypatch.setattr(host,'_JOB_SERVICE',None)
    monkeypatch.setattr(host,'_PROVIDER_CONTEXT',{'compute_provider_ids':['test']})
    monkeypatch.setattr(host,'_default_service',compose)
    actor=Actor(7,'tenant','/owned')
    with pytest.raises(RuntimeError):host.read_compute_catalog(actor,'test')
    assert seen==[(actor,'test')] and closed==[True]


def test_cancelled_http_read_joins_provider_before_return(monkeypatch):
    import asyncio
    from threading import Event
    from quart import Quart, g
    from routes.response_assets import response_resources
    entered=Event();released=Event()
    def read(*args,cancelled,**kwargs):
        entered.set()
        while not cancelled():released.wait(.01)
        released.set()
        raise InterruptedError('cancelled')
    monkeypatch.setattr(service,'read_resources',read)
    async def scenario():
        app=Quart(__name__)
        async with app.test_request_context('/api/response-surfaces/resources?runId=owned&provider=test'):
            g.auth_ctx=SimpleNamespace(is_authenticated=True,owner_user_id=7,key_id='test',tenant_id='',scopes=[])
            task=asyncio.create_task(response_resources())
            assert await asyncio.to_thread(entered.wait,2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,2)
            assert released.is_set()
    asyncio.run(scenario())


def test_headless_publication_rejects_conversation_references(monkeypatch):
    from lib.response_assets import validate_rich
    scopes=[]
    monkeypatch.setattr(service,'resolve_scope',lambda scope,**kw:scopes.append((scope,kw)) or '/owned')
    payload={'version':1,'title':'Status','blocks':[{'type':'notice','id':'note','title':'Status','tone':'info','message':'Awaiting evidence'}]}
    validate_rich(payload,conv_id=None,user_id=7,run_id='run')
    assert scopes==[({'runId':'run'},{'user_id':7})]
    payload['blocks']=[{'type':'form_prompt','id':'form','title':'Budget','interactionId':'unowned'}]
    with pytest.raises(PermissionError):validate_rich(payload,conv_id=None,user_id=7,run_id='run')
