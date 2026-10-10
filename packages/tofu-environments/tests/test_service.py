"""Executable provider API conformance and lifecycle fault cases."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import threading
import time as time
import pytest
pytestmark = pytest.mark.unit
from tofu_environments import Actor, EnvironmentError, EnvironmentService, MemoryRepository, Snapshot

@pytest.fixture
def spec():
    return {"profile":"project_development", "project":"example", "image":{"base":"base:1", "name":"example", "instructions":"RUN true"}, "workspace":{"name":"dev", "cpu":2, "memory_gib":4}}

class FakeProvider:
    provider_id = "test"
    operation_timeout = 10
    def __init__(self): self.calls=[]; self.remote={}; self.unknown=False; self.admission=1
    def validate(self, actor, spec): return {"binding_revision":self.admission}
    def create_image(self, actor, spec, op):
        self.calls.append('image'); self.remote[op]='image-1'
        if self.unknown: raise EnvironmentError('lost_reply', 'unknown', uncertain=True)
        return 'image-1'
    def start_build(self, *args, **kwargs): self.calls.append('build')
    def image_status(self, *args): return Snapshot('ready')
    def create_workspace(self, *args): self.calls.append('workspace'); return 'workspace-1'
    def activate_workspace(self, *args): self.calls.append('activate'); return True
    def workspace_status(self, *args): return Snapshot('ready', {"url":"https://workspace.example/"})
    def reconcile(self, actor, record):
        if record['operation_id'] in self.remote: return {"phase":"image_created","image_id":self.remote[record['operation_id']]}
        return None

def make():
    provider=FakeProvider(); repo=MemoryRepository(); service=EnvironmentService(repo,lambda actor,key:provider)
    return provider,repo,service,Actor(11,'tenant-a','/project')

def test_complete_flow_and_duplicate_requests(spec):
    provider,repo,service,actor=make(); plan=service.plan(actor,'test',spec,'request')
    assert service.plan(actor,'test',spec,'request') == plan
    for expected in ['image_created','building']:
        assert service.apply(actor,plan['operation_id'],plan['plan_hash'],lambda *_:True)['phase']==expected
    assert service.status(actor,plan['operation_id'])['phase']=='image_ready'
    for expected in ['workspace_created','activating']:
        assert service.apply(actor,plan['operation_id'],plan['plan_hash'],lambda *_:True)['phase']==expected
    assert service.status(actor,plan['operation_id'])['phase']=='ready'
    service.apply(actor,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    assert provider.calls==['image','build','workspace','activate']
    assert plan['durable'] is False

def test_exact_approval_owner_tenant_and_project(spec):
    provider,repo,service,actor=make(); plan=service.plan(actor,'test',spec,'request')
    for hash_,allow in [(plan['plan_hash'],False),('0'*64,True)]:
        with pytest.raises(EnvironmentError,match='authorization'):
            service.apply(actor,plan['operation_id'],hash_,lambda *_:allow)
    for other in [Actor(12,'tenant-a','/project'),Actor(11,'tenant-b','/project')]:
        with pytest.raises(EnvironmentError,match='owner scope'): service.status(other,plan['operation_id'])
    with pytest.raises(EnvironmentError,match='different project'): service.status(Actor(11,'tenant-a','/other'),plan['operation_id'])
    assert provider.calls==[]

def test_changed_scope_request_or_binding_never_mutates(spec):
    provider,repo,service,actor=make(); plan=service.plan(actor,'test',spec,'request')
    changed=deepcopy(spec); changed['workspace']['cpu']=4
    with pytest.raises(EnvironmentError,match='different plan'):service.plan(actor,'test',changed,'request')
    provider.admission=2
    with pytest.raises(EnvironmentError,match='admission changed'):service.apply(actor,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    assert provider.calls==[]

def test_lost_ack_reconciles_after_service_restart_without_replay(spec):
    provider,repo,service,actor=make(); provider.unknown=True
    plan=service.plan(actor,'test',spec,'request')
    result=service.apply(actor,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    assert result['phase']=='uncertain'
    other=EnvironmentService(repo,lambda actor,key:provider)
    assert other.apply(actor,plan['operation_id'],plan['plan_hash'],lambda *_:True)['phase']=='uncertain'
    assert other.status(actor,plan['operation_id'])['phase']=='image_created'
    assert provider.calls==['image']

def test_checkpoint_failure_retains_inflight_and_does_not_resubmit(spec,monkeypatch):
    provider,repo,service,actor=make(); plan=service.plan(actor,'test',spec,'request')
    original=repo.put
    def fail(actor,op,value,version):
        if value['phase']=='image_created':raise EnvironmentError('offline','Storage unavailable')
        return original(actor,op,value,version)
    monkeypatch.setattr(repo,'put',fail)
    with pytest.raises(EnvironmentError):service.apply(actor,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    assert service.status(actor,plan['operation_id'])['phase']=='creating_image'
    assert service.apply(actor,plan['operation_id'],plan['plan_hash'],lambda *_:True)['phase']=='creating_image'
    assert provider.calls==['image']
    monkeypatch.setattr(repo,'put',original)
    row=repo.get(actor,plan['operation_id']);row['value']['claim_until']=0
    repo.put(actor,plan['operation_id'],row['value'],row['version'])
    assert service.status(actor,plan['operation_id'])['phase']=='image_created'

def test_concurrent_apply_has_one_cas_claim(spec):
    provider,repo,service,actor=make(); plan=service.plan(actor,'test',spec,'request')
    entered=threading.Event();release=threading.Event();original=provider.create_image
    def slow(*args):entered.set();release.wait(3);return original(*args)
    provider.create_image=slow
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(service.apply,actor,plan['operation_id'],plan['plan_hash'],lambda *_:True)
        assert entered.wait(1)
        assert service.apply(actor,plan['operation_id'],plan['plan_hash'],lambda *_:True)['phase']=='creating_image'
        assert service.status(actor,plan['operation_id'])['phase']=='creating_image'
        release.set();assert first.result()['phase']=='image_created'
    assert provider.calls==['image']

@pytest.mark.parametrize('mutation',[lambda s:s.update(extra=True),lambda s:s['workspace'].update(cpu=0),lambda s:s['workspace'].update(memory_gib=float('nan')),lambda s:s['workspace'].update(gpu_count=1)])
def test_contract_rejects_invalid_specs(spec,mutation):
    provider,repo,service,actor=make();mutation(spec)
    with pytest.raises(EnvironmentError):service.plan(actor,'test',spec,'request')
    assert provider.calls==[]

def test_failed_provider_does_not_expose_exception_secrets(spec):
    provider,repo,service,actor=make();plan=service.plan(actor,'test',spec,'request')
    def fail(*_):raise RuntimeError('SECRET_PROVIDER_TOKEN')
    provider.create_image=fail
    result=service.apply(actor,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    assert result['phase']=='uncertain' and 'SECRET_PROVIDER_TOKEN' not in str(result)


def test_forged_approval_preview_is_rejected_before_side_effect(spec):
    provider,repo,service,actor=make();plan=service.plan(actor,'test',spec,'request')
    wrong=deepcopy(plan['plan']);wrong['spec']['workspace']['cpu']=100
    with pytest.raises(EnvironmentError,match='Presented approval'):
        service.apply(actor,plan['operation_id'],plan['plan_hash'],lambda *_:True,approved_plan=wrong)
    assert not provider.calls

def test_reconciliation_cannot_skip_to_ready_or_another_write_stage(spec):
    provider,repo,service,actor=make();provider.unknown=True
    plan=service.plan(actor,'test',spec,'request');service.apply(actor,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    for phase in ['ready','workspace_created']:
        provider.reconcile=lambda *_:{'phase':phase,'workspace_id':'wrong-stage'}
        with pytest.raises(EnvironmentError,match='phase'):service.status(actor,plan['operation_id'])
    assert provider.calls==['image']
