"""Discovery executor claims are exact, owner-bound and never stolen by age."""
import pytest
from lib.research import discovery_owner as owners,budget_binding as binding
from lib.storage.errors import StorageError
from tests.research.test_research_budget_binding import bound_source as bound_source,LIMITS
from tests.research.test_research_record_ack import research_task
from tests.research.test_research_workspace_evidence_guard import storage
pytestmark=pytest.mark.unit

def setup(source):
    task,client=source
    binding.bind_model_budget(task['task_id'],user_id=12,execution_limits=LIMITS)
    return task,client

def test_one_current_claim_and_foreign_owner_refused(bound_source):
    task,client=setup(bound_source)
    claim=owners.claim_discovery_execution(task['task_id'],user_id=12);claim.assert_current()
    with pytest.raises(StorageError,match='takeover refused'):
        owners.claim_discovery_execution(task['task_id'],user_id=12)
    with pytest.raises(ValueError,match='owned durable'):
        owners.inspect_discovery_execution(task['task_id'],user_id=13)
    assert not owners.inspect_discovery_execution(task['task_id'],user_id=12)['replay_allowed']

def test_false_ack_prevents_claim_and_lost_ack_reads_back(bound_source,monkeypatch):
    task,client=setup(bound_source);command=client.command
    def false(name,args,command_id):
        if args.get('namespace')==owners.NAMESPACE:return {'version':1}
        return command(name,args,command_id)
    monkeypatch.setattr(client,'command',false)
    with pytest.raises(StorageError,match='exactly acknowledged'):
        owners.claim_discovery_execution(task['task_id'],user_id=12)
    def lost(name,args,command_id):
        value=command(name,args,command_id)
        if args.get('namespace')==owners.NAMESPACE:
            raise StorageError('database_unavailable','synthetic lost ACK')
        return value
    monkeypatch.setattr(client,'command',lost)
    owners.claim_discovery_execution(task['task_id'],user_id=12).assert_current()

@pytest.mark.parametrize('state',['dead','unknown'])
def test_process_inspection_never_grants_takeover(bound_source,monkeypatch,state):
    task,client=setup(bound_source);owners.claim_discovery_execution(task['task_id'],user_id=12)
    monkeypatch.setattr(owners,'owner_state',lambda value:state)
    assert owners.inspect_discovery_execution(task['task_id'],user_id=12)=={
        'found':True,'process_state':state,'replay_allowed':False}
    with pytest.raises(StorageError,match='takeover refused'):
        owners.claim_discovery_execution(task['task_id'],user_id=12)

def test_process_incarnation_change_fences_claim(bound_source,monkeypatch):
    task,client=setup(bound_source);claim=owners.claim_discovery_execution(task['task_id'],user_id=12)
    monkeypatch.setattr(owners,'process_owner',lambda:None)
    with pytest.raises(StorageError,match='no longer owns'):claim.assert_current()

def test_concurrent_claims_have_one_winner(bound_source,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    task,client=setup(bound_source);command=client.command;barrier=threading.Barrier(2)
    def contend(name,args,command_id):
        if args.get('namespace')==owners.NAMESPACE:barrier.wait(timeout=5)
        return command(name,args,command_id)
    monkeypatch.setattr(client,'command',contend)
    def claim(_):
        try:return owners.claim_discovery_execution(task['task_id'],user_id=12)
        except StorageError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(claim,range(2)))
    winners=[c for c in results if c is not None]
    assert len(winners)==1
    winners[0].assert_current()

def test_terminal_task_fences_existing_claim(bound_source):
    from lib.research import records
    task,client=setup(bound_source)
    claim=owners.claim_discovery_execution(task['task_id'],user_id=12)
    task['status']='done';records.persist_record(task)
    with pytest.raises(StorageError,match='no longer owns'):claim.assert_current()
    with pytest.raises(ValueError,match='terminal'):
        owners.claim_discovery_execution(task['task_id'],user_id=12)
