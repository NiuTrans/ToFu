"""Shared-root budget checkpoint carries all spent credit, not new authority."""
import threading
import pytest
from jsonschema.exceptions import ValidationError
from lib.agent_core.execution_budget import ExecutionBudget, ModelBudgetExceeded

pytestmark=pytest.mark.unit
PROFILE=dict(encoding='cl100k_base',output_field='max_tokens',wire_model='fixture')

def fresh(*,owner=7,cap=100,tool_cap=3,deadline=None):
    task=dict(id='root',_userId=owner,abort_event=threading.Event(),config={'executionLimits':dict(max_tokens=cap,max_tool_calls=tool_cap,deadline=deadline)})
    account=ExecutionBudget(task);task['_executionBudget']=account
    return task,account

def capture(task,account):return account.capture_shared_account(owner=7,task=task,counter_policy=PROFILE)

def restored(snapshot,**options):
    task,account=fresh(**options)
    account.restore_shared_account(snapshot,owner=7,task=task,counter_policy=PROFILE)
    return task,account

def test_all_spent_credit_and_ordinals_survive_one_time_restore():
    task,account=fresh()
    account.bind_model_attempt_observer(lambda event: None,tool_observer=lambda event: None)
    lease=account.reserve_model(task,input_tokens=10,max_output_tokens=50)
    lease.mark_dispatched();lease.settle({'prompt_tokens':20,'completion_tokens':30})
    for ordinal in range(2):
        assert account.reserve_tool(task) is None
        attempt=account.begin_tool_attempt(task,str(ordinal),'lookup',0)
        account.finish_tool_attempt(attempt,phase='returned')
    child=dict(id='child',_userId=7,config={});account.attach(child)
    assert account.reserve_tool(child) is None
    account.close(child)
    snapshot=capture(task,account)
    assert snapshot['tool_calls']==3 and snapshot['control_tool_calls']==2
    assert snapshot['tool_ordinal']==2
    task,account=restored(snapshot)
    assert account.consumption_snapshot()==(50,3,True)
    lease=account.reserve_model(task,input_tokens=10,max_output_tokens=100)
    assert lease.ordinal==2 and lease.max_output_tokens==40
    lease.settle(unsent=True)
    assert account.reason(pending_tools=1)=='execution_tool_budget_exhausted'
    with pytest.raises(ValueError,match='pristine'):account.restore_shared_account(snapshot,owner=7,task=task,counter_policy=PROFILE)

@pytest.mark.parametrize('change',[{'owner_user_id':8},{'root_task_id':'foreign'},{'tokens':True},{'control_tool_calls':4,'tool_calls':3},
    {'limits':dict(max_tokens=101,max_tool_calls=3,deadline=None)}, {'counter_policy':dict(PROFILE,wire_model='other')}])
def test_invalid_scope_or_changed_policy_never_mutates_pristine_account(change):
    task,account=fresh();snapshot=capture(task,account);snapshot.update(change)
    with pytest.raises((ValueError, ValidationError)):account.restore_shared_account(snapshot,owner=7,task=task,counter_policy=PROFILE)
    assert account.consumption_snapshot()==(0,0,True)
    assert account.continuity_snapshot()['pending_model_requests']==0

def test_active_child_and_unsettled_model_requests_cannot_export():
    task,account=fresh();child=dict(id='child',_userId=7,config={});account.attach(child)
    with pytest.raises(ValueError,match='quiescent'):capture(task,account)
    account.close(child);lease=account.reserve_model(task,input_tokens=1,max_output_tokens=1)
    with pytest.raises(ValueError,match='quiescent'):capture(task,account)
    lease.settle(unsent=True);assert capture(task,account)['model_ordinal']==1

def test_unknown_usage_and_original_deadline_are_preserved():
    task,account=fresh(deadline=2000000000.125);lease=account.reserve_model(task,input_tokens=1,max_output_tokens=1)
    lease.mark_dispatched();lease.settle(None)
    snapshot=capture(task,account);assert not snapshot['complete']
    task,account=restored(snapshot,deadline=2000000000.125)
    with pytest.raises(ModelBudgetExceeded,match='usage_unknown'):account.reserve_model(task,input_tokens=1,max_output_tokens=1)
    assert capture(task,account)['limits']['deadline']==2000000000.125
    with pytest.raises(ValueError):restored(snapshot,deadline=2000000001)

def test_any_prior_admission_refuses_credit_reset():
    task,account=fresh();snapshot=capture(task,account);lease=account.reserve_model(task,input_tokens=1,max_output_tokens=1);lease.settle(unsent=True)
    with pytest.raises(ValueError,match='pristine'):account.restore_shared_account(snapshot,owner=7,task=task,counter_policy=PROFILE)


@pytest.mark.parametrize('fault',[None,'missing_authority','changed_usage','changed_tokens','prior_admission','child_usage'])
def test_hydrated_paid_usage_reconciles_once_without_resetting_credit(fault):
    task,account=fresh()
    usage={'prompt_tokens':20,'completion_tokens':30}
    lease=account.reserve_model(task,input_tokens=20,max_output_tokens=30)
    lease.mark_dispatched();lease.settle(usage)
    snapshot=capture(task,account)
    task,account=fresh()
    task['usage']=dict(usage)
    task['_checkpointUsage']=dict(usage)
    if fault=='missing_authority':task.pop('_checkpointUsage')
    elif fault=='changed_usage':task['usage']['completion_tokens']+=1
    elif fault=='changed_tokens':snapshot['tokens']+=1
    elif fault=='prior_admission':
        lease=account.reserve_model(task,input_tokens=1,max_output_tokens=1)
        lease.settle(unsent=True)
    elif fault=='child_usage':account._settled_tokens=1
    before=account.consumption_snapshot()
    if fault is not None:
        with pytest.raises(ValueError,match='pristine'):
            account.restore_shared_account(snapshot,owner=7,task=task,counter_policy=PROFILE)
        assert account.consumption_snapshot()==before
    else:
        account.restore_shared_account(snapshot,owner=7,task=task,counter_policy=PROFILE)
        assert account.consumption_snapshot()==(50,0,True)
        assert capture(task,account)==snapshot
        with pytest.raises(ValueError,match='pristine'):
            account.restore_shared_account(snapshot,owner=7,task=task,counter_policy=PROFILE)
        lease=account.reserve_model(task,input_tokens=10,max_output_tokens=100)
        assert lease.max_output_tokens==40 and lease.ordinal==2
        lease.settle(unsent=True)


def test_pending_tool_intent_refuses_capture_until_return_and_restores_ordinal():
    task,account=fresh()
    account.bind_model_attempt_observer(lambda event: None,tool_observer=lambda event: None)
    assert account.reserve_tool(task) is None
    attempt=account.begin_tool_attempt(task,'call','lookup',0)
    with pytest.raises(ValueError,match='quiescent'):capture(task,account)
    account.finish_tool_attempt(attempt,phase='returned')
    snapshot=capture(task,account)
    assert snapshot['tool_ordinal']==1 and snapshot['tool_calls']==1
    task,account=restored(snapshot)
    assert account.tool_calls_for_control(task)==1
    assert account.consumption_snapshot()==(0,1,True)


def test_proved_pending_tool_consumption_never_recreates_budget_or_changes_model_cost():
    task, account = fresh()
    snapshot = capture(task, account)
    task, account = restored(snapshot)
    account.reconcile_pending_tools(snapshot, owner=7, task=task, count=2)
    after = capture(task, account)
    assert after == dict(snapshot, tool_calls=2, control_tool_calls=2)
    assert task['_executionToolsAdmitted'] == 2
    with pytest.raises(ValueError, match='exact'):
        account.reconcile_pending_tools(snapshot, owner=7, task=task, count=2)
    assert account.reserve_tool(task) is None
    assert account.reserve_tool(task) == 'execution_tool_budget_exhausted'


@pytest.mark.parametrize('fault', ['count', 'owner', 'overspend', 'observer', 'not_restored'])
def test_pending_tool_consumption_refuses_unproven_or_changed_account(fault):
    task, account = fresh()
    if fault == 'observer':
        account.bind_model_attempt_observer(lambda event: None, tool_observer=lambda event: None)
    snapshot = capture(task, account)
    if fault not in {'observer', 'not_restored'}:
        task, account = restored(snapshot)
    with pytest.raises(ValueError):
        account.reconcile_pending_tools(snapshot, owner=8 if fault=='owner' else 7, task=task,
            count=True if fault=='count' else 4 if fault=='overspend' else 1)
    assert capture(task, account) == snapshot


def test_unknown_pending_tools_remain_incomplete_and_cannot_spend_as_zero():
    task, account = fresh()
    snapshot = capture(task, account)
    task, account = restored(snapshot)
    account.refuse_pending_tool_recovery(snapshot, owner=7, task=task)
    assert account.consumption_snapshot() == (0, 0, False)
    with pytest.raises(ModelBudgetExceeded):
        account.reserve_model(task, input_tokens=1, max_output_tokens=1)
