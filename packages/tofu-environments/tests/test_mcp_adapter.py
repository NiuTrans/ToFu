"""MCP uses the real SDK ownership, exact approval and no-replay boundaries."""
from test_jobs import context as context
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import pytest
from tofu_environments import Actor, EnvironmentService, EnvironmentError, MemoryRepository
from tofu_environments.mcp_adapter import MCPBinding
from tofu_environments import host
from test_service import FakeProvider
from test_host import SPEC

pytestmark = pytest.mark.unit


def test_unconfigured_mcp_and_native_capabilities_are_not_advertised():
    assert MCPBinding(Actor(1)).schemas() == []
    ctx = SimpleNamespace(owner_user_id=1)
    assert {r['function']['name'] for r in host._schemas(ctx)} == {'environment_providers'}


def test_native_compute_wait_and_collection_require_their_actual_context():
    host.configure_jobs(object())
    try:
        ctx = SimpleNamespace(owner_user_id=1, cfg={}, project_ready=False)
        names = {r['function']['name'] for r in host._compute_schemas(ctx)}
        assert 'compute_catalog' in names and 'compute_wait' not in names and 'compute_collect' not in names
        ctx.cfg={'evolution': {'run_id': 'run', 'stage': 'executing'}};ctx.project_ready=True
        names = {r['function']['name'] for r in host._compute_schemas(ctx)}
        assert {'compute_wait', 'compute_collect'} <= names
    finally:
        host.configure_jobs(None)


def test_environment_plan_apply_uses_bound_identity_and_exact_preview():
    provider=FakeProvider();service=EnvironmentService(MemoryRepository(), lambda *_:provider)
    binding=MCPBinding(Actor(11, 'tenant', '/project'), environment_service=service)
    plan=binding.invoke('environment_plan', {'provider':'test','spec':SPEC,'request_id':'request'})
    args={k:plan[k] for k in ('operation_id','plan_hash','plan')}
    with pytest.raises(EnvironmentError, match='authorization'):
        binding.invoke('environment_apply',args)
    assert not provider.calls
    with pytest.raises(EnvironmentError):
        binding.invoke('environment_status',{'operation_id':plan['operation_id'],'owner_id':11})
    other=replace(binding,actor=Actor(12,'tenant','/project'))
    with pytest.raises(EnvironmentError):
        other.invoke('environment_status',{'operation_id':plan['operation_id']})
    grants=[deepcopy(args)]
    def authorize(actor,name,preview):
        assert actor==binding.actor and name=='environment_apply'
        return bool(grants and grants.pop()==preview)
    approved=replace(binding,authorize=authorize)
    assert approved.invoke('environment_apply',args)['phase']=='image_created'
    with pytest.raises(EnvironmentError):approved.invoke('environment_apply',args)
    changed=deepcopy(args);changed['plan']['spec']['workspace']['cpu']=99
    with pytest.raises(EnvironmentError):approved.invoke('environment_apply',changed)
    assert provider.calls==['image']


def test_compute_uncertain_submission_reconciles_without_another_job(context):
    provider,repository,service,actor,spec,plan=context
    provider.lost=True
    args={k:plan[k] for k in ('operation_id','plan_hash','plan')}
    calls=[]
    binding=MCPBinding(actor,job_service=service,
                       authorize=lambda a,n,v: calls.append((a,n,v)) is None)
    assert 'compute_wait' not in {r['function']['name'] for r in binding.schemas()}
    with pytest.raises(EnvironmentError):
        replace(binding,authorize=None).invoke('compute_submit',args)
    assert provider.calls==0
    assert binding.invoke('compute_submit',args)['phase']=='uncertain'
    assert binding.invoke('compute_submit',args)['phase']=='uncertain'
    assert binding.invoke('compute_status',{'operation_id':plan['operation_id']})['phase']=='succeeded'
    assert provider.calls==1


def test_mcp_cannot_select_a_collection_destination(context):
    provider,repository,service,actor,spec,plan=context
    binding=MCPBinding(actor,job_service=service)
    with pytest.raises(EnvironmentError):
        binding.invoke('compute_collect',{'operation_id':plan['operation_id'],'destination':'/elsewhere'})
