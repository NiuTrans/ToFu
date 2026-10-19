"""Host integration: real approval receipts, schema/entrypoint and storage CAS."""
import json
from types import SimpleNamespace
import pytest
pytestmark = pytest.mark.unit
from tofu_environments import Actor, EnvironmentError, EnvironmentService, MemoryRepository
from tofu_environments import host
from tofu_environments.repository import HostRepository, MANIFEST
from test_service import FakeProvider

SPEC={'profile':'project_development','project':'example','image':{'base':'base:1','name':'example','instructions':'RUN true'},'workspace':{'name':'dev','cpu':2,'memory_gib':4}}

def test_plugin_declaration_and_no_owner_visibility():
    specs=[];host.register(specs.append);host.register_compute(specs.append)
    env, compute = specs
    assert env.write_tools == env.confirmation_tools == env.auto_approval_tools == frozenset({'environment_apply'})
    assert compute.write_tools == compute.confirmation_tools == compute.auto_approval_tools == frozenset({'compute_submit', 'compute_cancel'})
    assert not env.provides & compute.provides
    assert host._schemas(SimpleNamespace(owner_user_id=0)) == []
    assert {r['function']['name'] for r in host._schemas(SimpleNamespace(owner_user_id=1))} == {'environment_providers'}
    from tofu_environments.schemas import tool_schemas
    assert {r['function']['name'] for r in tool_schemas()} == env.provides | compute.provides
    assert env.discovery_policy == compute.discovery_policy == 'searchable'


def test_handler_uses_exact_one_use_gateway_receipt_and_discloses_preview():
    from lib.tasks_pkg.tool_dispatch._approval import mint_approval_receipt, enrich_approval_metadata
    provider=FakeProvider();service=EnvironmentService(MemoryRepository(),lambda *_:provider)
    host.configure(service)
    try:
        task={'_userId':11};actor=Actor(11,project_root='/project')
        plan=service.plan(actor,'test',SPEC,'request')
        args={k:plan[k] for k in ('operation_id','plan_hash','plan')}
        invoke=lambda call:json.loads(host._handler(task,None,'environment_apply',call,args,1,{}, {},'/project',True)[1])
        assert invoke('unapproved')['code']=='approval_required' and not provider.calls
        meta={};enrich_approval_metadata(meta,'environment_apply',args)
        assert 'RUN true' in json.dumps(meta['riskFields']) and 'memory_gib' in json.dumps(meta)
        mint_approval_receipt(task,'environment_apply','approved',args,authority='human')
        assert invoke('approved')['phase']=='image_created'
        assert invoke('approved')['code']=='approval_required'
        mint_approval_receipt(task,'environment_apply','changed',args,authority='human')
        args['plan']['spec']['workspace']['cpu']=99
        assert invoke('changed')['ok'] is False and provider.calls==['image']
    finally:host.configure(None)

def test_durable_repository_cas_owner_and_restart(tmp_path):
    from lib.storage import StorageSupervisor
    from lib.storage.manifest import validate_manifest
    validate_manifest(MANIFEST)
    supervisor=StorageSupervisor(project_root=tmp_path,backend='sqlite',startup_timeout=60)
    supervisor.start()
    try:
        client=supervisor.client
        client.command('plugin.register',{'manifest':MANIFEST},'register-environments')
        repo=HostRepository(lambda **_:client);actor=Actor(11,'tenant')
        first=repo.put(actor,'operation',{'phase':'creating_image'},0)
        assert first['version']==1 and repo.get(Actor(12,'tenant'),'operation') is None
        with pytest.raises(EnvironmentError,match='concurrently'):repo.put(actor,'operation',{},0)
        supervisor.stop();supervisor.start();client=supervisor.client
        assert HostRepository(lambda **_:client).get(actor,'operation')=={'version':1,'value':{'phase':'creating_image'}}
    finally:supervisor.stop()

def test_public_sdk_has_no_host_dependency_outside_adapter():
    import ast
    from pathlib import Path
    public=Path(__file__).resolve().parents[1]/'tofu_environments'
    for path in public.glob('*.py'):
        if path.name == 'host.py': continue
        imports=[]
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node,ast.Import): imports.extend(alias.name for alias in node.names)
            elif isinstance(node,ast.ImportFrom) and node.level==0: imports.append(node.module or '')
        assert all(not name.startswith('lib') for name in imports)


def test_stage_logs_expose_ids_not_recipe_or_exception_secrets(caplog):
    provider=FakeProvider();service=EnvironmentService(MemoryRepository(),lambda *_:provider)
    actor=Actor(11);plan=service.plan(actor,'test',SPEC,'log-case')
    with caplog.at_level('INFO',logger='tofu.environments'):
        service.apply(actor,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    assert 'creating_image' in caplog.text and 'image_created' in caplog.text
    assert 'RUN true' not in caplog.text

def test_real_host_registry_admits_plugin_metadata(monkeypatch):
    from dataclasses import replace
    from lib.tools.registry import _spec
    monkeypatch.setattr(_spec,'_TOOL_SPECS',[])
    monkeypatch.setattr(_spec,'_REGISTERED_KEYS',set())
    registered=[];host.register(registered.append)
    _spec.register_tool_spec(replace(registered[0],source='plugin',plugin_name='environments'))
    assert _spec.all_specs()[0].plugin_name=='environments'

def test_capacity_scales_with_memory_and_volume_and_missing_probe_is_lean():
    small=host._capacity_defaults(SimpleNamespace(effective_memory_available_mb=32,disk_free_mb=16))
    large=host._capacity_defaults(SimpleNamespace(effective_memory_available_mb=32768,disk_free_mb=131072))
    missing=host._capacity_defaults(SimpleNamespace(effective_memory_available_mb=None,disk_free_mb=None))
    assert large['context_byte_limit']>small['context_byte_limit']
    assert large['context_file_limit']>small['context_file_limit']
    assert large['output_limit']>small['output_limit']
    assert small['context_byte_limit']==4*1048576 and missing['context_byte_limit']==1048576

def test_compute_native_gateway_exact_approval_and_owner(context):
    from lib.tasks_pkg.tool_dispatch._approval import mint_approval_receipt, enrich_approval_metadata
    provider,repo,service,actor,spec,plan=context
    host.configure_jobs(service)
    task={'_userId':actor.owner_id,'_tenantId':actor.tenant_id}
    args={k:plan[k] for k in ('operation_id','plan_hash','plan')}
    def invoke(name,call,arguments=args,owner_task=task):
        return json.loads(host._handler(owner_task,None,name,call,arguments,1,{}, {},actor.project_root,True)[1])
    try:
        assert invoke('compute_submit','missing')['code']=='approval_required'
        assert provider.calls==0
        meta={};enrich_approval_metadata(meta,'compute_submit',args)
        assert 'gpu_seconds' in json.dumps(meta['riskFields'])
        mint_approval_receipt(task,'compute_submit','approved',args,authority='human')
        assert invoke('compute_submit','approved')['phase']=='queued'
        assert invoke('compute_submit','approved')['code']=='approval_required'
        status_args={'operation_id':plan['operation_id']}
        assert invoke('compute_status','status',status_args)['phase']=='succeeded'
        assert invoke('compute_status','foreign',status_args,{'_userId':2})['code']=='not_found'
        assert provider.calls==1
    finally:host.configure_jobs(None)

def test_compute_preview_tamper_does_not_submit(context):
    from lib.tasks_pkg.tool_dispatch._approval import mint_approval_receipt
    from copy import deepcopy
    provider,repo,service,actor,spec,plan=context;host.configure_jobs(service)
    task={'_userId':actor.owner_id,'_tenantId':actor.tenant_id}
    args=deepcopy({k:plan[k] for k in ('operation_id','plan_hash','plan')})
    args['plan']['spec']['resources']['gpu_count']=99
    mint_approval_receipt(task,'compute_submit','approved',args,authority='human')
    try:
        result=json.loads(host._handler(task,None,'compute_submit','approved',args,1,{}, {},actor.project_root,True)[1])
        assert result['code']=='approval_scope_changed' and provider.calls==0
    finally:host.configure_jobs(None)

# Reuse the batch fixture so host tests exercise the exact same job contract.
from test_jobs import context as context

def test_native_compute_cancel_requires_its_own_exact_receipt(context):
    from lib.tasks_pkg.tool_dispatch._approval import mint_approval_receipt
    p,r,s,a,spec,plan=context;host.configure_jobs(s);calls=[];p.cancel_job=lambda *args:calls.append(args)
    s.submit(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    task={'_userId':a.owner_id,'_tenantId':a.tenant_id};args={k:plan[k] for k in ('operation_id','plan_hash','plan')}
    def invoke(call):return json.loads(host._handler(task,None,'compute_cancel',call,args,1,{}, {},a.project_root,True)[1])
    try:
        assert invoke('missing')['code']=='approval_required'
        mint_approval_receipt(task,'compute_cancel','approved',args,authority='human')
        assert invoke('approved')['phase']=='cancelling'
        assert invoke('approved')['code']=='approval_required'
        assert len(calls)==1
    finally:host.configure_jobs(None)


def test_compute_collect_tool_delivers_owned_results_and_cleans_failure(context,tmp_path):
    from pathlib import Path
    from tofu_environments.jobs import JobService
    provider,repo,_,_,spec,_=context
    service=JobService(repo,lambda *_:provider);actor=Actor(11,'tenant',str(tmp_path))
    plan=service.plan(actor,'test',spec,'collect-native');service.submit(actor,plan['operation_id'],plan['plan_hash'],lambda *_:True);service.status(actor,plan['operation_id'])
    provider.read_job_output=lambda *args:iter([b'{"ok":true}'])
    host.configure_jobs(service)
    def invoke(owner=11,project=str(tmp_path),enabled=True):
        return json.loads(host._handler({'_userId':owner,'_tenantId':'tenant'},None,'compute_collect','collect',{'operation_id':plan['operation_id']},1,{}, {},project,enabled)[1])
    try:
        result=invoke();assert result['ok'] and result['total_bytes']==11
        directory=Path(result['directory']);assert tmp_path in directory.parents
        assert (directory/'smoke-result.json').read_bytes()==b'{"ok":true}'
        assert directory.parent.stat().st_mode & 0o777==0o700
        before=set(tmp_path.iterdir());assert invoke(owner=12)['code']=='not_found';assert set(tmp_path.iterdir())==before
        assert invoke(enabled=False)['code']=='project_required';assert set(tmp_path.iterdir())==before
        provider.read_job_output=lambda *args:iter([b'changed'])
        assert invoke()['code']=='output_changed';assert set(tmp_path.iterdir())==before
        specs=[];host.register_compute(specs.append);assert 'compute_collect' in specs[0].provides
        from tofu_environments.schemas import tool_schemas
        schema=next(x['function'] for x in tool_schemas() if x['function']['name']=='compute_collect')
        assert set(schema['parameters']['properties'])=={'operation_id'}
    finally:host.configure_jobs(None)

@pytest.mark.parametrize('fail',[False,True])
def test_default_compute_call_supplies_repository_and_closes_registered_resources(monkeypatch,tmp_path,fail,context):
    import tempfile
    import lib.storage
    import runtime_guards
    provider,repo,service,actor,spec,plan=context
    monkeypatch.setattr(host,'_JOB_SERVICE',None)
    monkeypatch.setattr(host,'HostRepository',lambda source:repo)
    class Client:
        def command(self,*args,**kw):return {}
    monkeypatch.setattr(lib.storage,'get_storage_client',lambda **kw:Client())
    monkeypatch.setattr(runtime_guards,'probe_system_resources',lambda:SimpleNamespace(effective_memory_available_mb=32,disk_free_mb=16))
    paths=[];owners=[]
    def factory(a,c):
        assert c['repository'] is repo
        owners.append(a)
        staging=tempfile.TemporaryDirectory(dir=tmp_path);paths.append(staging.name)
        c['register_cleanup'](staging.cleanup)
        class Provider:
            def validate_job(self,a,s,*,operation_id):
                assert operation_id.startswith('job-')
                if fail:raise EnvironmentError('provider_unconfigured','missing trusted binding')
                return {'revision':1}
        return Provider()
    ep=SimpleNamespace(name='hope',load=lambda:factory)
    monkeypatch.setattr(host,'entry_points',lambda **kw:[ep])
    args={'provider':'hope','spec':spec,'request_id':'call-lifecycle'}
    result=json.loads(host._handler({'_userId':actor.owner_id,'_tenantId':actor.tenant_id},None,'compute_plan','call',args,1,{}, {},actor.project_root,True)[1])
    assert result['ok'] is (not fail)
    assert owners==[actor]
    from pathlib import Path
    assert paths and all(not Path(p).exists() for p in paths)

def test_injected_job_service_lifecycle_stays_with_embedding(monkeypatch):
    class Service:
        closed=False
        def status(self,*args):return {'phase':'running'}
        def close(self):self.closed=True
    service=Service();monkeypatch.setattr(host,'_JOB_SERVICE',service)
    result=json.loads(host._handler({'_userId':7},None,'compute_status','call',{'operation_id':'job-7'},1,{}, {},'/project',True)[1])
    assert result['phase']=='running' and not service.closed
