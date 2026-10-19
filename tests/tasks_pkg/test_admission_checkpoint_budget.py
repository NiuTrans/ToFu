"""Dispatch prerequisites retry transient pressure with finite RPC deadlines until completion or cancellation."""
import time
import pytest
from lib.storage import StorageError
from lib.tasks_pkg.manager._persist import _upsert_task_row
from lib.task_replay.task_result_checkpoint_contract import TASK_RESULT_CHECKPOINT_GUARD_CONTRACT

pytestmark=pytest.mark.unit


def write(task,budget=5,status='pending'):
    return _upsert_task_row(task,'',content='',thinking='',status=status,error_json=None,
        tr_json=None,meta_json=None,admission_budget_seconds=budget)


def clock(monkeypatch):
    now=[100.0]
    monkeypatch.setattr(time,'monotonic',lambda:now[0])
    monkeypatch.setattr(time,'sleep',lambda amount:now.__setitem__(0,now[0]+amount))
    return now


def test_slow_initial_witness_and_transient_retry_share_admission_budget(monkeypatch):
    now=clock(monkeypatch);calls=[];reads=[0]
    class Client:
        def query(self,operation,payload,**options):
            assert payload['user_id']==7
            calls.append((operation,options['deadline']));reads[0]+=1;now[0]+=.6
            if reads[0]==1:raise StorageError('database_timeout','temporary queue delay')
            return None
        def command(self,operation,payload,command_id,**options):
            calls.append((operation,options['deadline']))
            assert payload['expected_version']==0 and payload['value']['user_id']==7
            return {'owned':True,'version':1,'guard_contract':TASK_RESULT_CHECKPOINT_GUARD_CONTRACT}
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **_:Client())
    assert write({'id':'birth','convId':'','_userId':7})
    assert len(calls)==3
    assert [value for _,value in calls]==[5,5,5]


def test_transient_write_retries_preserve_pending_identity_and_deadline(monkeypatch):
    now=clock(monkeypatch);calls=[]
    class Client:
        def command(self,operation,payload,command_id,**options):
            calls.append((dict(payload),options['deadline']));now[0]+=.6
            if len(calls)==1:raise StorageError('database_busy','pressure')
            return {'owned':True,'version':2,'guard_contract':TASK_RESULT_CHECKPOINT_GUARD_CONTRACT}
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **_:Client())
    task={'id':'birth','convId':'','_userId':7,'_taskResultVersion':1,
        '_taskResultCheckpointGuard':TASK_RESULT_CHECKPOINT_GUARD_CONTRACT}
    assert write(task)
    assert len(calls)==2 and calls[1][1]==calls[0][1]==5
    assert calls[0][0]==calls[1][0]


def test_slow_witness_preserves_admission_and_starts_bounded_write(monkeypatch):
    now=clock(monkeypatch);writes=[]
    class Client:
        def query(self,*args,**options):now[0]+=5.1;return None
        def command(self,*args,**options):
            assert options['deadline']==5
            writes.append(args)
            return {'owned':True,'version':1,'guard_contract':TASK_RESULT_CHECKPOINT_GUARD_CONTRACT}
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **_:Client())
    assert write({'id':'birth','convId':'','_userId':7})
    assert len(writes)==1


@pytest.mark.parametrize('budget',[0,-1,float('inf'),float('nan'),True])
def test_admission_budget_requires_finite_positive_value(monkeypatch,budget):
    with pytest.raises(ValueError):write({'id':'birth','_userId':7},budget=budget)


def test_admission_policy_cannot_be_used_for_running_diagnostics():
    with pytest.raises(ValueError):write({'id':'birth','_userId':7},status='running')


def test_abort_fence_survives_admission_conflict_refresh(monkeypatch):
    clock(monkeypatch);writes=[]
    class Client:
        def query(self,*args,**options):return {'version':2,'status':'pending','abort_requested_at':17,'abort_source':'owner'}
        def command(self,operation,payload,command_id,**options):
            writes.append(dict(payload))
            if len(writes)==1:raise StorageError('database_conflict','abort raced')
            assert payload['expected_version']==2
            assert payload['value']['abort_requested_at']==17 and payload['value']['abort_source']=='owner'
            return {'owned':True,'version':3,'guard_contract':TASK_RESULT_CHECKPOINT_GUARD_CONTRACT}
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **_:Client())
    assert write({'id':'birth','convId':'','_userId':7,'_taskResultVersion':1,
        '_taskResultCheckpointGuard':TASK_RESULT_CHECKPOINT_GUARD_CONTRACT})
    assert len(writes)==2
