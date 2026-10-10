"""Startup discovery is paged, owner-explicit and isolated from refused jobs."""
from copy import deepcopy
import pytest
from lib.research.action_startup import resume_interrupted_actions
from lib.research.action_observations import ActionObservations,BINDING_NAMESPACE,_digest
from lib.research.execution_owner import ActionExecutionOwners,NAMESPACE
from lib.process_owner import process_owner
from lib.storage.errors import StorageError
from tests.research.test_research_action_observations import MemoryRecords

pytestmark=pytest.mark.unit


def test_paged_sweep_preserves_owners_and_isolates_ineligible_records(monkeypatch):
    client=MemoryRecords();base=process_owner()
    if base is None:pytest.skip('local process incarnation unavailable')
    basis={'hypothesis':'saved','protocol':{'baseline':'reference'}}
    bindings={}
    for n in range(24):
        owner=1+n%2;task_id='startup-action-'+str(n)
        repo=ActionObservations(client,user_id=owner,task_id=task_id,direction='Scientific objective',
            lang='en',revision=1,basis_digest=_digest(basis));repo.freeze_basis(basis)
        bindings[n]=repo
        if n==0:continue
        stamp=base if n==1 else None if n==2 else {**base,'pid':base['pid']+10000000+n}
        monkeypatch.setattr('lib.research.execution_owner.process_owner',lambda stamp=stamp:stamp)
        ActionExecutionOwners.open(client,user_id=owner,task_id=task_id).claim_initial()
    from lib.research.action_terminal import NAMESPACE as TERMINAL
    client.rows[TERMINAL,bindings[5].prefix]={'version':1,'value':{}}
    client.rows[BINDING_NAMESPACE,bindings[6].prefix]['version']=2
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **kwargs:client)
    live={(bindings[4].binding['task_id'],bindings[4].binding['user_id'])}
    monkeypatch.setattr('lib.research.action_runtime._research_action_runtime.get_owned',
        lambda task_id,*,user_id: {'status':'running'} if (task_id,user_id) in live else None)
    attempts=[]
    def resume(task_id,*,user_id):
        attempts.append((task_id,user_id))
        if task_id=='startup-action-3':raise StorageError('database_conflict','incomplete paid frontier')
        live.add((task_id,user_id));return {'resumed':True,'deduped':False}
    monkeypatch.setattr('lib.research.action_continuation.resume_action',resume)
    reads=[];original_query=client.query
    def query(operation,payload):
        reads.append((operation,deepcopy(payload)));return original_query(operation,payload)
    client.query=query
    before=deepcopy(client.rows)
    assert resume_interrupted_actions()==17
    assert len(attempts)==19
    assert all(owner==1+int(task_id.rsplit('-',1)[1])%2 for task_id,owner in attempts)
    pages=[payload for operation,payload in reads if operation=='record.list']
    assert len(pages)==4 and all(payload['limit']==16 for payload in pages)
    assert [payload['namespace'] for payload in pages[:2]]==['research.pending_action_bindings.v1']*2
    assert resume_interrupted_actions()==0
    assert attempts[-1]==('startup-action-3',2)
    assert client.rows==before


@pytest.mark.parametrize('rows',[None,[{'key':None}],[{'key':''}]])
def test_invalid_scan_page_never_invents_a_task(monkeypatch,rows):
    class InvalidClient:
        def query(self,*args):return rows
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **kwargs:InvalidClient())
    monkeypatch.setattr('lib.research.action_continuation.resume_action',lambda *args,**kwargs:pytest.fail('bad page resumed a task'))
    with pytest.raises(StorageError):resume_interrupted_actions()


def test_shutdown_stops_the_sweep_without_starting_another_actor(monkeypatch):
    from threading import Event
    event=Event();event.set()
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **kwargs:pytest.fail('shutdown sweep acquired storage'))
    assert resume_interrupted_actions(stop_event=event)==0


def test_shutdown_during_page_read_does_not_admit_the_returned_row(monkeypatch):
    from threading import Event
    event=Event()
    class StoppingClient:
        def query(self,*args):event.set();return [{'key':'returned-after-stop'}]
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **kwargs:StoppingClient())
    monkeypatch.setattr('lib.research.action_continuation.resume_action',lambda *args,**kwargs:pytest.fail('shutdown admitted an actor'))
    assert resume_interrupted_actions(stop_event=event)==0
