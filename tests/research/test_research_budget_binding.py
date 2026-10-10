"""Research model policy requires an exact owned immutable durable binding."""
from copy import deepcopy
import pytest
from lib.research import records,budget_binding as budget
from lib.evolution import records as record_store
from lib.storage.errors import StorageError
from tests.research.test_research_record_ack import research_task as research_task
from tests.research.test_research_workspace_evidence_guard import storage
pytestmark=pytest.mark.unit
LIMITS={'max_tokens':1000,'max_tool_calls':None,'deadline':2000000000}

@pytest.fixture
def bound_source(research_task,monkeypatch):
    task,client=research_task
    monkeypatch.setattr(record_store,'_storage',lambda **kwargs:client)
    records.persist_record(task)
    return task,client

def test_immutable_policy_and_foreign_owner(bound_source):
    task,client=bound_source;tid=task['task_id']
    first=budget.bind_model_budget(tid,user_id=12,execution_limits=LIMITS)
    assert budget.bind_model_budget(tid,user_id=12,execution_limits=LIMITS)==first
    assert budget.read_model_budget(tid,user_id=12)==first
    changed={**LIMITS,'max_tokens':2000}
    with pytest.raises(StorageError,match='immutable'):
        budget.bind_model_budget(tid,user_id=12,execution_limits=changed)
    with pytest.raises(ValueError,match='owned durable'):
        budget.read_model_budget(tid,user_id=13)
    assert budget.read_model_budget(tid,user_id=12)==first
    assert len(budget._encode(first))<=4096

def test_false_ack_and_lost_ack(bound_source,monkeypatch):
    task,client=bound_source;command=client.command
    def false(name,args,command_id):
        if args.get('namespace')==budget.NAMESPACE:return {'version':1}
        return command(name,args,command_id)
    monkeypatch.setattr(client,'command',false)
    with pytest.raises(StorageError,match='exactly acknowledged'):
        budget.bind_model_budget(task['task_id'],user_id=12,execution_limits=LIMITS)
    assert budget.read_model_budget(task['task_id'],user_id=12) is None
    def lost(name,args,command_id):
        result=command(name,args,command_id)
        if args.get('namespace')==budget.NAMESPACE:
            raise StorageError('database_unavailable','synthetic lost acknowledgement')
        return result
    monkeypatch.setattr(client,'command',lost)
    value=budget.bind_model_budget(task['task_id'],user_id=12,execution_limits=LIMITS)
    assert budget.read_model_budget(task['task_id'],user_id=12)==value

def test_changed_source_refuses_binding_read(bound_source):
    task,client=bound_source
    value=budget.bind_model_budget(task['task_id'],user_id=12,execution_limits=LIMITS)
    task['direction']='Changed source';records.persist_record(task)
    with pytest.raises(StorageError,match='identity changed'):
        budget.read_model_budget(task['task_id'],user_id=12)

def test_terminal_source_refuses_new_binding(bound_source):
    task,client=bound_source;task['status']='done';records.persist_record(task)
    with pytest.raises(ValueError,match='terminal'):
        budget.bind_model_budget(task['task_id'],user_id=12,execution_limits=LIMITS)

@pytest.mark.parametrize('policy',[{**LIMITS,'max_tokens':True},{**LIMITS,'deadline':float('inf')},
    {**LIMITS,'max_tool_calls':1},{**LIMITS,'max_tokens':-1}])
def test_invalid_policy_refuses_write(bound_source,policy):
    task,client=bound_source
    with pytest.raises(ValueError,match='invalid'):
        budget.bind_model_budget(task['task_id'],user_id=12,execution_limits=policy)
    assert budget.read_model_budget(task['task_id'],user_id=12) is None
