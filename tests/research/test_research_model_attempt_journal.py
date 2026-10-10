"""Physical Research accounting acknowledges intents and refuses fresh replay."""
from copy import deepcopy
import pytest
from lib.research import model_attempt_journal as journal,budget_binding as binding
from lib.agent_core.execution_budget import model_budget_admission
from lib.storage.errors import StorageError
from tests.research.test_research_budget_binding import bound_source as bound_source,LIMITS
from tests.research.test_research_record_ack import research_task
from tests.research.test_research_workspace_evidence_guard import storage
pytestmark=pytest.mark.unit

def root(task):
    return {'id':task['task_id'],'_userId':12,'config':{'executionLimits':deepcopy(LIMITS)}}

def prepare(bound_source):
    task,client=bound_source
    binding.bind_model_budget(task['task_id'],user_id=12,execution_limits=LIMITS)
    carrier=root(task);budget=journal.install_model_attempt_journal(carrier)
    return task,client,carrier,budget

def test_intent_and_terminal_keep_exact_consumption(bound_source):
    task,client,carrier,budget=prepare(bound_source)
    lease=model_budget_admission(carrier)(input_tokens=10,max_output_tokens=20)
    assert journal.read_model_attempt(task['task_id'],1,'dispatch_intent',user_id=12) is None
    lease.mark_dispatched()
    intent=journal.read_model_attempt(task['task_id'],1,'dispatch_intent',user_id=12)
    assert intent['reserved_tokens']==30 and not intent['usage_complete']
    lease.settle({'prompt_tokens':8,'completion_tokens':3})
    terminal=journal.read_model_attempt(task['task_id'],1,'terminal',user_id=12)
    assert terminal['producer_id']==intent['producer_id']
    assert terminal['reported_tokens']==terminal['account_observed_tokens']==11
    assert terminal['usage_complete']
    assert budget.consumption_snapshot()==(11,0,True)
    with pytest.raises(StorageError,match='explicit recovery'):
        journal.install_model_attempt_journal(root(task))

def test_false_ack_prevents_send_and_fences_new_intent(bound_source,monkeypatch):
    task,client,carrier,budget=prepare(bound_source);command=client.command
    def refuse(name,args,command_id):
        if args.get('namespace')==journal.NAMESPACE and args['value']['phase']=='dispatch_intent':
            return {'version':1}
        return command(name,args,command_id)
    monkeypatch.setattr(client,'command',refuse)
    lease=model_budget_admission(carrier)(input_tokens=1,max_output_tokens=2)
    with pytest.raises(StorageError,match='exactly acknowledged'):lease.mark_dispatched()
    assert not lease.dispatched
    lease.settle(unsent=True)
    assert journal.read_model_attempt(task['task_id'],1,'terminal',user_id=12)['phase']=='voided'
    next_lease=model_budget_admission(carrier)(input_tokens=1,max_output_tokens=2)
    with pytest.raises(StorageError):next_lease.mark_dispatched()
    assert not next_lease.dispatched
    next_lease.settle(unsent=True)

def test_lost_ack_can_confirm_only_original_producer(bound_source,monkeypatch):
    task,client,carrier,budget=prepare(bound_source);command=client.command
    def lost(name,args,command_id):
        result=command(name,args,command_id)
        if args.get('namespace')==journal.NAMESPACE:
            raise StorageError('database_unavailable','synthetic lost ACK')
        return result
    monkeypatch.setattr(client,'command',lost)
    lease=model_budget_admission(carrier)(input_tokens=1,max_output_tokens=2)
    lease.mark_dispatched();lease.settle({'prompt_tokens':1,'completion_tokens':2})
    assert budget.consumption_snapshot()==(3,0,True)
    assert journal.read_model_attempt(task['task_id'],1,'terminal',user_id=12)['reported_tokens']==3

def test_concurrent_fresh_producers_cannot_share_send_identity(bound_source):
    task,client=bound_source
    binding.bind_model_budget(task['task_id'],user_id=12,execution_limits=LIMITS)
    first,second=root(task),root(task)
    journal.install_model_attempt_journal(first);journal.install_model_attempt_journal(second)
    a=model_budget_admission(first)(input_tokens=1,max_output_tokens=2)
    b=model_budget_admission(second)(input_tokens=1,max_output_tokens=2)
    a.mark_dispatched()
    with pytest.raises(StorageError,match='another producer'):b.mark_dispatched()
    assert not b.dispatched
    a.settle({'prompt_tokens':1,'completion_tokens':2})
    b.settle(unsent=True)
    assert second['_executionBudget'].consumption_snapshot()==(0,0,False)
    assert journal.read_model_attempt(task['task_id'],1,'terminal',user_id=12)['reported_tokens']==3

def test_missing_usage_keeps_unknown_terminal(bound_source):
    task,client,carrier,budget=prepare(bound_source)
    lease=model_budget_admission(carrier)(input_tokens=1,max_output_tokens=2)
    lease.mark_dispatched();lease.settle(None)
    terminal=journal.read_model_attempt(task['task_id'],1,'terminal',user_id=12)
    assert not terminal['usage_complete'] and terminal['reported_tokens']==0
    assert budget.consumption_snapshot()==(0,0,False)

def test_terminal_ack_failure_retains_paid_lower_bound(bound_source,monkeypatch):
    task,client,carrier,budget=prepare(bound_source);command=client.command
    def refuse(name,args,command_id):
        if args.get('namespace')==journal.NAMESPACE and args['value']['phase']=='settled':
            return {'version':1}
        return command(name,args,command_id)
    monkeypatch.setattr(client,'command',refuse)
    lease=model_budget_admission(carrier)(input_tokens=10,max_output_tokens=20)
    lease.mark_dispatched();lease.settle({'prompt_tokens':8,'completion_tokens':3})
    assert budget.consumption_snapshot()==(11,0,False)
    assert journal.read_model_attempt(task['task_id'],1,'terminal',user_id=12) is None
    with pytest.raises(StorageError,match='explicit recovery'):
        journal.install_model_attempt_journal(root(task))

def test_owner_mutation_cannot_dispatch(bound_source):
    task,client,carrier,budget=prepare(bound_source)
    lease=model_budget_admission(carrier)(input_tokens=1,max_output_tokens=2)
    carrier['_userId']=True
    with pytest.raises((TypeError,ValueError)):lease.mark_dispatched()
    assert not lease.dispatched
    lease.settle(unsent=True)
    assert budget.consumption_snapshot()==(0,0,False)

def test_real_concurrent_intent_cas_has_one_winner(bound_source,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    task,client=bound_source
    binding.bind_model_budget(task['task_id'],user_id=12,execution_limits=LIMITS)
    roots=[root(task),root(task)]
    for carrier in roots:journal.install_model_attempt_journal(carrier)
    leases=[model_budget_admission(carrier)(input_tokens=1,max_output_tokens=2) for carrier in roots]
    command=client.command;barrier=threading.Barrier(2)
    def contend(name,args,command_id):
        if args.get('namespace')==journal.NAMESPACE and args['value']['phase']=='dispatch_intent':
            barrier.wait(timeout=5)
        return command(name,args,command_id)
    monkeypatch.setattr(client,'command',contend)
    def mark(lease):
        try:lease.mark_dispatched();return True
        except StorageError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:winners=list(pool.map(mark,leases))
    assert sorted(winners)==[False,True]
    for won,lease in zip(winners,leases):
        lease.settle({'prompt_tokens':1,'completion_tokens':2} if won else None,unsent=not won)
    terminal=journal.read_model_attempt(task['task_id'],1,'terminal',user_id=12)
    assert terminal['reported_tokens']==3

def test_record_key_and_event_ordinal_must_match(bound_source):
    task,client,carrier,budget=prepare(bound_source)
    lease=model_budget_admission(carrier)(input_tokens=1,max_output_tokens=2)
    lease.mark_dispatched();lease.settle({'prompt_tokens':1,'completion_tokens':2})
    event=journal.read_model_attempt(task['task_id'],1,'terminal',user_id=12)
    event['attempt_ordinal']=2
    client.command('record.put',{'namespace':journal.NAMESPACE,
        'key':journal._key(task['task_id'],1,'terminal',12),'value':event},'synthetic-corrupt-event')
    with pytest.raises(StorageError,match='binding changed'):
        journal.read_model_attempt(task['task_id'],1,'terminal',user_id=12)
