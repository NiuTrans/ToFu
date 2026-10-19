"""Original Goal tool charges survive reconciliation without replay or refund."""
from copy import deepcopy

import pytest

from lib.agent_core.execution_budget import ModelBudgetExceeded
from tests.agent_core.test_native_goal_handoff import prepared as prepared, consume
from tests.agent_core.test_recovered_actor_prefix import spend
from tests.agent_core.test_shared_account_checkpoint import PROFILE

pytestmark = pytest.mark.unit


def pending(prepared, charged=1):
    handoff, storage, budget, policies, cuts = prepared
    consume(handoff, policies)
    task = policies[0][0].task
    state = budget.capture_actor_account(owner=7, task=task, counter_policy=PROFILE)
    state['tool_calls'] -= charged
    state['control_tool_calls'] -= charged
    return budget, task, state, cuts


def verify(budget, task, state, **overrides):
    return budget.verify_recovered_actor_pending_tools(state, **{
        'owner':7, 'task':task, 'source_task_id':'source-a', 'count':1, 'counter_policy':PROFILE, **overrides})


@pytest.mark.parametrize('charged', [0, 1])
def test_proven_batch_accounts_only_unpaid_calls_and_preserves_model_credit(prepared, charged):
    budget, task, state, cuts = pending(prepared, charged)
    assert verify(budget, task, state)==dict(source_root_task_id='source', charged_tool_calls=charged)
    before=budget.consumption(); saved=deepcopy(state); length=len(cuts)
    budget.reconcile_recovered_actor_tools(state, owner=7, task=task, source_task_id='source-a', count=1)
    assert state==saved and budget.consumption()==(before[0], before[1]+1-charged)
    assert len(cuts)==length+1-charged
    if not charged:
        assert cuts[-1]['reason']=='tools_reconciled'
        from lib.agent_core.account_frontier import validate_frontier, validate_recovery_transition
        acknowledged=prepared[1].calls[0][1]['goal_handoff']['authorization']['account_frontiers'][-1]
        validate_frontier(cuts[-1]);validate_recovery_transition(acknowledged,cuts[-1])
        with pytest.raises(ValueError):verify(budget, task, state)
    else:
        budget.reconcile_recovered_actor_tools(state, owner=7, task=task, source_task_id='source-a', count=1)
        assert budget.consumption()==before


@pytest.mark.parametrize('damage', ['source','owner','count','tokens','control','ordinal','limits','policy','partial','new_spend','observer','failed_ack'])
def test_changed_or_unacknowledged_actor_cannot_reconcile(prepared, damage):
    budget, task, state, _ = pending(prepared)
    options={}
    if damage=='source':options['source_task_id']='source-b'
    elif damage=='owner':options['owner']=8
    elif damage=='count':options['count']=True
    elif damage=='tokens':state['tokens']+=1
    elif damage=='control':state['control_tool_calls']+=1
    elif damage=='ordinal':state['tool_ordinal']+=1
    elif damage=='limits':state['limits']['max_tokens']+=1
    elif damage=='policy':state['counter_policy']['wire_model']='foreign'
    elif damage=='partial':options['count']=2
    elif damage=='new_spend':spend(task, 0, 1)
    elif damage=='observer':budget._tool_observer=lambda event:None
    else:budget._account_frontier_failed=True
    before=budget.consumption()
    with pytest.raises(ValueError):verify(budget, task, state, **options)
    assert budget.consumption()==before


def test_failed_reconciliation_publication_retains_charge_and_fences_execution(prepared):
    budget, task, state, _ = pending(prepared, 0)
    def refuse(frontier):raise OSError('commit outcome unknown')
    budget._account_frontier_observer=refuse
    with pytest.raises(OSError):budget.reconcile_recovered_actor_tools(
        state, owner=7, task=task, source_task_id='source-a', count=1)
    assert budget.consumption()==(36,6)
    with pytest.raises(ModelBudgetExceeded):budget.reserve_model(task,input_tokens=0,max_output_tokens=1)


@pytest.mark.parametrize('unbounded',[False,True])
def test_unknown_actor_effect_preserves_paid_quota_and_fences_whole_account(prepared,unbounded):
    budget, task, _, _ = pending(prepared)
    if unbounded:budget.limits.update(max_tokens=None,max_tool_calls=None)
    budget.refuse_recovered_actor_tools(owner=7, task=task)
    assert budget.consumption_snapshot()==(36,5,False)
    assert budget.reason()=='execution_account_frontier_unavailable'
    with pytest.raises(ModelBudgetExceeded):budget.reserve_model(task,input_tokens=0,max_output_tokens=1)
    assert budget.reserve_tool(task)=='execution_account_frontier_unavailable'


@pytest.mark.parametrize('actor_cap',[False, True])
def test_unpaid_reconciliation_cannot_exceed_global_or_actor_tool_caps(prepared,actor_cap):
    budget, task, state, cuts = pending(prepared,0)
    if actor_cap:task['config']['executionLimits']={'max_tool_calls':2}
    before=budget.consumption(); length=len(cuts)
    with pytest.raises(ValueError,match='budget'):
        budget.reconcile_recovered_actor_tools(state,owner=7,task=task,source_task_id='source-a',count=1 if actor_cap else 4)
    assert budget.consumption()==before and len(cuts)==length
