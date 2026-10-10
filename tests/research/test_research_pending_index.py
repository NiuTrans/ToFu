"""Pending lookup cannot hide or erase durable Research execution evidence."""
from copy import deepcopy
import pytest
from lib.agent_core.task_runtime import TaskRuntime
from lib.research.action_pending import NAMESPACE,retain_pending,retire_settled
from lib.research.action_terminal import persist_action_terminal,load_action_terminal
from lib.research.action_startup import resume_interrupted_actions
from lib.research.action_observations import BINDING_NAMESPACE,_task_prefix
from lib.storage.errors import StorageError
from tests.research.test_research_action_observations import MemoryRecords,intent_repository
from tests.research.test_research_execution_owner import repository
from tests.research.test_research_action_terminal import create
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark=pytest.mark.unit


class CacheRecords(MemoryRecords):
    def __init__(self):
        super().__init__();self.refuse_cleanup=False;self.lose_delete_ack=False
    def command(self,operation,payload,idempotency_key):
        if operation!='record.delete':return super().command(operation,payload,idempotency_key)
        assert payload['namespace']==NAMESPACE
        if self.refuse_cleanup:raise StorageError('database_unavailable','cache cleanup unavailable')
        existed=self.rows.pop((NAMESPACE,payload['key']),None) is not None
        if self.lose_delete_ack:raise StorageError('database_unavailable','lost delete acknowledgment')
        return {'deleted':existed}


def test_index_ack_and_retirement_never_replace_original_journals(monkeypatch):
    client=CacheRecords();repo=repository(client);before=deepcopy(client.rows)
    client.lose_ack=True
    claim=repo.claim_initial();claim.assert_current()
    assert all(client.rows[key]==value for key,value in before.items())
    assert client.rows[NAMESPACE,repo.prefix]['value']['binding']==repo.binding
    with pytest.raises(StorageError):repo.claim_initial()
    assert retire_settled(client,task_id=repo.binding['task_id'],user_id=1) is False
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **kwargs:client)
    runtime=TaskRuntime('pending-index-terminal',terminal_persist=persist_action_terminal)
    task=create(runtime,repo.binding['task_id'])
    runtime.update_fields(task['id'],fields={'direction':'Frozen basis','expected_revision':7,
        '_committedWorkspaceRevision':8})
    client.refuse_cleanup=True
    assert runtime.finish(task['id'],result={'actual':'durable'})
    assert task['status']=='done' and (NAMESPACE,repo.prefix) in client.rows
    terminal=load_action_terminal(task['id'],user_id=1)['terminal']
    assert terminal['status']=='done'
    durable={key:deepcopy(value) for key,value in client.rows.items() if key[0]!=NAMESPACE}
    client.refuse_cleanup=False;client.lose_delete_ack=True
    assert retire_settled(client,task_id=task['id'],user_id=1)
    assert (NAMESPACE,repo.prefix) not in client.rows
    assert durable==client.rows
    # Cache can be rebuilt without reusing the deleted row's acknowledgment.
    retain_pending(client,repo.binding)
    assert (NAMESPACE,repo.prefix) in client.rows
    assert retire_settled(client,task_id=task['id'],user_id=1)
    assert durable==client.rows
    runtime.remove_owned(task['id'],user_id=1)


@pytest.mark.parametrize('fault',['missing','malformed_page','binding'])
def test_bad_index_falls_back_to_original_owned_binding(monkeypatch,fault):
    import lib.research.execution_owner as owners
    from lib.process_owner import process_owner
    client=CacheRecords();base=process_owner()
    if base is None:pytest.skip('process identity unavailable')
    monkeypatch.setattr(owners,'process_owner',lambda:{**base,'pid':base['pid']+10000000})
    repo=repository(client);repo.claim_initial()
    if fault=='missing':client.rows.pop((NAMESPACE,repo.prefix))
    elif fault=='binding':client.rows[NAMESPACE,repo.prefix]['value']['binding']['revision']+=1
    query=client.query
    if fault=='malformed_page':
        client.query=lambda operation,payload:None if operation=='record.list' and payload['namespace']==NAMESPACE else query(operation,payload)
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **kwargs:client)
    admitted=[]
    monkeypatch.setattr('lib.research.action_runtime._research_action_runtime.get_owned',
        lambda task_id,*,user_id:{'status':'running'} if admitted else None)
    def resume(task_id,*,user_id):
        admitted.append((task_id,user_id));return {'resumed':True,'deduped':False}
    monkeypatch.setattr('lib.research.action_continuation.resume_action',resume)
    assert resume_interrupted_actions()==1
    assert admitted==[(repo.binding['task_id'],1)]


def test_indexed_action_is_admitted_before_large_settled_history(monkeypatch,record_property):
    import lib.research.execution_owner as owners
    from lib.process_owner import process_owner
    from lib.research.action_terminal import NAMESPACE as TERMINAL
    client=CacheRecords();base=process_owner()
    if base is None:pytest.skip('process identity unavailable')
    monkeypatch.setattr(owners,'process_owner',lambda:{**base,'pid':base['pid']+10000000})
    repo=repository(client);repo.claim_initial()
    for n in range(2048):
        task_id='settled-history-'+str(n);binding=dict(repo.binding,task_id=task_id)
        key=_task_prefix(1,task_id)[1]
        client.rows[BINDING_NAMESPACE,key]={'version':1,'value':{'schema_version':1,'binding':binding}}
        client.rows[TERMINAL,key]={'version':1,'value':{}}
    reads=[];query=client.query
    def measured(operation,payload):
        reads.append((operation,payload['namespace']));return query(operation,payload)
    client.query=measured
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **kwargs:client)
    live=[]
    monkeypatch.setattr('lib.research.action_runtime._research_action_runtime.get_owned',
        lambda task_id,*,user_id:{'status':'running'} if live and task_id==repo.binding['task_id'] else None)
    def resume(task_id,*,user_id):
        assert task_id==repo.binding['task_id']
        assert not any(operation=='record.list' and ns==BINDING_NAMESPACE for operation,ns in reads)
        assert len(reads)==5
        record_property('rpcs_before_first_indexed_resume_with_2048_settlements',len(reads))
        live.append(task_id);return {'resumed':True,'deduped':False}
    monkeypatch.setattr('lib.research.action_continuation.resume_action',resume)
    assert resume_interrupted_actions()==1


def test_pending_retirement_survives_selected_store_restart(storage,monkeypatch):
    supervisor,_=storage;repo=repository(supervisor.client);repo.claim_initial()
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **kwargs:supervisor.client)
    runtime=TaskRuntime('selected-pending-index',terminal_persist=persist_action_terminal)
    task=create(runtime,repo.binding['task_id'])
    runtime.update_fields(task['id'],fields={'direction':'Frozen basis','expected_revision':7})
    runtime.finish(task['id'],result={'retained':'result'})
    supervisor.stop();supervisor.start()
    assert supervisor.client.query('record.get',{'namespace':NAMESPACE,'key':repo.prefix}) is None
    assert load_action_terminal(task['id'],user_id=1)['terminal']['result']=={'retained':'result'}
    assert retire_settled(supervisor.client,task_id=task['id'],user_id=1)
    runtime.remove_owned(task['id'],user_id=1)
