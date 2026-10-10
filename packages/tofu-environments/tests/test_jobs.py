"""Batch submission authority, checkpoint loss and provider status contracts."""
from copy import deepcopy
import pytest
from tofu_environments.contracts import Actor, EnvironmentError
from tofu_environments.repository import MemoryRepository
from tofu_environments.jobs import JobService
pytestmark = pytest.mark.unit

class Provider:
    def __init__(self): self.calls = 0; self.lost = False; self.revision = 1; self.reference = None; self.snapshot = {'state':'succeeded','exit_code':0}
    def validate_job(self, actor, spec, *, operation_id): return {'revision':self.revision}
    def submit_job(self, actor, spec, operation_id, admission):
        self.calls += 1; self.reference = {'kind':'hope_run_id','id':'run-1'}
        if self.lost: raise TimeoutError()
        return self.reference
    def reconcile_job(self, actor, record): return self.reference
    def job_status(self, actor, reference, operation_id): return self.snapshot

@pytest.fixture
def context():
    provider = Provider(); repository = MemoryRepository(); service = JobService(repository, lambda *_: provider)
    actor = Actor(1, 'tenant', '/project')
    spec = {'version':1,'project':'demo','command':['python3','gpu_smoke.py'],
            'source':{'sha256':'a'*64,'uri':'artifact:source'},'resources':{'cpu':2,'memory_gib':8,'gpu_count':1},
            'limits':{'runtime_seconds':180,'gpu_seconds':180,'result_bytes':1048576},
            'outputs':['smoke-result.json'],'provider_options':{}}
    plan = service.plan(actor, 'hope', spec, 'request')
    return provider, repository, service, actor, spec, plan

def submit(context):
    p,r,s,a,spec,plan=context
    return s.submit(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)

def test_complete_flow_and_idempotency(context):
    p,r,s,a,spec,plan=context
    assert s.plan(a,'hope',spec,'request')==plan
    assert submit(context)['phase']=='queued'
    assert submit(context)['phase']=='queued'
    assert s.status(a,plan['operation_id'])['phase']=='succeeded'
    assert p.calls==1

def test_failed_execution_can_be_closed_without_losing_failure_evidence(context):
    p,r,s,a,spec,plan=context;submit(context)
    p.snapshot={'state':'failed','exit_code':7}
    assert s.status(a,plan['operation_id'])['phase']=='failed'
    calls=[];p.cancel_job=lambda actor,reference,operation_id:calls.append((actor,reference,operation_id))
    assert s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)['phase']=='cancelling'
    assert len(calls)==1
    p.snapshot={'state':'cancelled','exit_code':None}
    assert s.status(a,plan['operation_id'])['phase']=='cancelled'
    failure=r.get(a,plan['operation_id'])['value']['execution_failure_before_cancellation']
    assert failure['phase']=='failed' and failure['exit_code']==7

def test_definitively_rejected_submission_without_reference_is_not_cancelled(context):
    p,r,s,a,spec,plan=context
    def reject(*args):raise EnvironmentError('submission_rejected','Explicit cluster rejection')
    p.submit_job=reject
    failed=submit(context);assert failed['phase']=='failed' and failed['reference'] is None
    def forbidden(*args):raise AssertionError('No external reference exists to cancel')
    p.cancel_job=forbidden
    assert s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)==failed
    assert r.get(a,plan['operation_id'])['value']['phase']=='failed'

def test_recovery_seals_restart_safety_and_all_attempt_gpu_budget(context):
    from tofu_environments.jobs import validate_job_spec
    spec=deepcopy(context[4])
    spec['recovery']={'max_attempts':3,'restart_safe':True,'allowed_reason':'scheduler_preemption'}
    with pytest.raises(EnvironmentError):validate_job_spec(spec)
    spec['limits']['gpu_seconds']=540
    assert validate_job_spec(spec)==spec
    spec['recovery']['restart_safe']=False
    with pytest.raises(EnvironmentError):validate_job_spec(spec)

def test_lost_response_is_read_only_reconciled(context):
    p,r,s,a,spec,plan=context;p.lost=True
    assert submit(context)['phase']=='uncertain'
    assert submit(context)['phase']=='uncertain'
    assert s.status(a,plan['operation_id'])['phase']=='succeeded'
    assert p.calls==1

def test_checkpoint_failure_and_new_service_do_not_replay(context,monkeypatch):
    p,r,s,a,spec,plan=context; original=r.put
    def put(actor,op,value,version):
        if value['phase']=='queued':raise RuntimeError('checkpoint failure')
        return original(actor,op,value,version)
    monkeypatch.setattr(r,'put',put)
    with pytest.raises(RuntimeError):submit(context)
    monkeypatch.setattr(r,'put',original)
    restarted=JobService(r,lambda *_:p)
    assert restarted.submit(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)['phase']=='submitting'
    assert restarted.status(a,plan['operation_id'])['phase']=='succeeded'
    assert p.calls==1

def test_authority_and_admission(context):
    p,r,s,a,spec,plan=context
    with pytest.raises(EnvironmentError,match='authorization'):s.submit(a,plan['operation_id'],plan['plan_hash'],lambda *_:False)
    with pytest.raises(EnvironmentError):s.status(Actor(2,'tenant','/project'),plan['operation_id'])
    with pytest.raises(EnvironmentError):s.status(Actor(1,'tenant','/other'),plan['operation_id'])
    p.revision=2
    with pytest.raises(EnvironmentError,match='admission changed'):submit(context)
    assert p.calls==0

@pytest.mark.parametrize('mutation',[lambda x:x['limits'].update(gpu_seconds=179),lambda x:x.update(command=[]),lambda x:x.update(outputs=['../escape']),lambda x:x['resources'].update(gpu_count=-1)])
def test_invalid_resource_and_output_contract(context,mutation):
    p,r,s,a,spec,plan=context;mutation(spec)
    with pytest.raises(EnvironmentError):s.plan(a,'hope',spec,'other')

@pytest.mark.parametrize('snapshot',[{'state':'succeeded','exit_code':None},{'state':'succeeded','exit_code':1},{'state':'succeeded','exit_code':False},{'state':'invented','exit_code':0}])
def test_success_requires_proven_exit(context,snapshot):
    p,r,s,a,spec,plan=context;submit(context);p.snapshot=snapshot
    with pytest.raises(EnvironmentError):s.status(a,plan['operation_id'])
    assert r.get(a,plan['operation_id'])['value']['phase']=='queued'

def test_conflicting_request(context):
    p,r,s,a,spec,plan=context;spec['command']=['other']
    with pytest.raises(EnvironmentError,match='another job'):s.plan(a,'hope',spec,'request')

def test_concurrent_submission_has_one_external_write(context):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    p,r,s,a,spec,plan=context; entered=Event(); release=Event(); original=p.submit_job
    def slow(*args):
        entered.set(); assert release.wait(5)
        return original(*args)
    p.submit_job=slow
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(submit,context)
        assert entered.wait(5)
        assert submit(context)['phase']=='submitting'
        release.set()
        assert first.result()['phase']=='queued'
    assert p.calls==1

def test_collect_hashes_owned_outputs_and_detects_changed_result(context,tmp_path):
    import hashlib
    p,r,s,a,spec,plan=context;submit(context);s.status(a,plan['operation_id'])
    calls=[]
    def read(actor,reference,operation_id,name):
        calls.append((actor,reference,operation_id,name));yield b'{"ok":true}'
    p.read_job_output=read
    receipt=s.collect(a,plan['operation_id'],tmp_path)
    assert receipt['outputs']==[{'name':'smoke-result.json','bytes':11,'sha256':hashlib.sha256(b'{"ok":true}').hexdigest()}]
    assert calls[0]==(a,p.reference,plan['operation_id'],'smoke-result.json')
    p.read_job_output=lambda *args: iter([b'changed'])
    with pytest.raises(EnvironmentError,match='differ'):s.collect(a,plan['operation_id'],tmp_path)
    assert len(list(tmp_path.iterdir()))==1

def test_collect_rejects_unfinished_and_foreign_job(context,tmp_path):
    p,r,s,a,spec,plan=context
    with pytest.raises(EnvironmentError,match='successful'):s.collect(a,plan['operation_id'],tmp_path)
    with pytest.raises(EnvironmentError):s.collect(Actor(2),plan['operation_id'],tmp_path)
    assert not list(tmp_path.iterdir())

@pytest.mark.parametrize('chunks',[[b'x'*1048577],[b'x'*600000,b'x'*600000],['text']])
def test_collect_bounds_and_cleans_partial_results(context,tmp_path,chunks):
    p,r,s,a,spec,plan=context;submit(context);s.status(a,plan['operation_id'])
    p.read_job_output=lambda *args:iter(chunks)
    with pytest.raises(EnvironmentError):s.collect(a,plan['operation_id'],tmp_path)
    assert not list(tmp_path.iterdir())
    assert 'outputs' not in r.get(a,plan['operation_id'])['value']

def test_collect_checkpoint_failure_cleans_partial(context,tmp_path,monkeypatch):
    p,r,s,a,spec,plan=context;submit(context);s.status(a,plan['operation_id'])
    p.read_job_output=lambda *args:iter([b'ok'])
    monkeypatch.setattr(r,'put',lambda *args: (_ for _ in ()).throw(RuntimeError('lost checkpoint')))
    with pytest.raises(RuntimeError):s.collect(a,plan['operation_id'],tmp_path)
    assert not list(tmp_path.iterdir())

@pytest.mark.parametrize('lost',[False,True])
def test_cancel_is_claimed_once_and_requires_terminal_readback(context,lost):
    p,r,s,a,spec,plan=context;submit(context);calls=[]
    def cancel(*args):
        calls.append(args)
        if lost:raise TimeoutError()
    p.cancel_job=cancel;p.snapshot={'state':'running','exit_code':None}
    result=s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    assert result['phase']==('cancel_uncertain' if lost else 'cancelling')
    assert s.status(a,plan['operation_id'])['phase']==result['phase']
    s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    assert len(calls)==1
    p.snapshot={'state':'cancelled','exit_code':None}
    assert s.status(a,plan['operation_id'])['phase']=='cancelled'

def test_cancel_rejects_foreign_or_unapproved_job(context):
    p,r,s,a,spec,plan=context;submit(context)
    with pytest.raises(EnvironmentError):s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:False)
    with pytest.raises(EnvironmentError):s.cancel(Actor(2),plan['operation_id'],plan['plan_hash'],lambda *_:True)

def test_cancel_checkpoint_loss_does_not_issue_stop_again(context,monkeypatch):
    p,r,s,a,spec,plan=context;submit(context);calls=[]
    p.cancel_job=lambda *args:calls.append(args)
    original=r.put
    def put(actor,op,value,version):
        if value['phase']=='cancelling' and version>=4:raise RuntimeError('checkpoint lost')
        return original(actor,op,value,version)
    monkeypatch.setattr(r,'put',put)
    with pytest.raises(RuntimeError):s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    monkeypatch.setattr(r,'put',original)
    restarted=JobService(r,lambda *_:p)
    assert restarted.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)['phase']=='cancelling'
    assert len(calls)==1

def test_provider_admission_receives_same_operation_at_plan_and_submit(context):
    p,r,s,a,spec,plan=context;seen=[]
    def validate(actor,spec,*,operation_id):
        seen.append(operation_id);return {'revision':p.revision}
    p.validate_job=validate
    assert s.plan(a,'hope',spec,'request')['operation_id']==plan['operation_id']
    submit(context)
    assert seen==[plan['operation_id'],plan['operation_id']]
