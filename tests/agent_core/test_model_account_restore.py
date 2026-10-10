"""Restored consumption and ordinals constrain subsequent physical admission."""
import pytest
from lib.agent_core.execution_budget import ExecutionBudget, ModelBudgetExceeded
pytestmark = pytest.mark.unit


def fresh(cap=100):
    task = {'id':'restored-root', 'config':{'executionLimits':{
        'max_tokens':cap, 'max_tool_calls':None, 'deadline':None}}}
    return task, ExecutionBudget(task)


def test_native_deadline_observation_is_exact_and_uses_the_shared_tree_limit():
    from lib.agent_core.execution_budget import execution_deadline_unix_ms
    unbounded = {'id': 'unbounded'}
    assert execution_deadline_unix_ms(unbounded) is None
    assert '_executionBudget' not in unbounded
    task, account = fresh()
    account.limits['deadline'] = 100.125
    child = {'id': 'child', '_executionBudget': account, 'config': {'executionLimits': {
        'max_tokens': None, 'max_tool_calls': None, 'deadline': 200}}}
    assert execution_deadline_unix_ms(child) == 100125
    child['config']['executionLimits']['deadline'] = 0
    assert execution_deadline_unix_ms(child) == 0
    child['config']['executionLimits']['deadline'] = True
    with pytest.raises(ValueError):
        execution_deadline_unix_ms(child)


def test_native_deadline_refuses_unrepresentable_time_and_unknown_account():
    from lib.agent_core.execution_budget import execution_deadline_unix_ms
    task, _ = fresh()
    task['config']['executionLimits']['deadline'] = 1e30
    with pytest.raises(ValueError, match='timestamp bounds'):
        execution_deadline_unix_ms(task)
    task['config']['executionLimits']['deadline'] = None
    task['_executionBudget'] = object()
    with pytest.raises(ValueError, match='canonical account'):
        execution_deadline_unix_ms(task)


def restore(account, **changes):
    values = dict(root_task_id='restored-root', consumed_tokens=70,
                  last_attempt_ordinal=4, usage_complete=True)
    account.restore_model_account(**{**values, **changes})


def test_recovered_consumption_limits_output_and_continues_journal_ordinals():
    task, account = fresh()
    events = []
    account.bind_model_attempt_observer(events.append)
    restore(account)
    assert account.consumption_snapshot() == (70,0,True)
    lease = account.reserve_model(task, input_tokens=10, max_output_tokens=50)
    assert lease.ordinal == 5 and lease.max_output_tokens == 20
    lease.mark_dispatched()
    lease.settle({'prompt_tokens':8,'completion_tokens':3})
    assert account.consumption_snapshot() == (81,0,True)
    assert [event['attempt_ordinal'] for event in events] == [5,5]
    assert events[-1]['account_observed_tokens'] == 81
    with pytest.raises(ValueError, match='pristine'):
        restore(account, consumed_tokens=0)


def test_unknown_recovered_usage_preserves_lower_bound_and_blocks_finite_send():
    task, account = fresh()
    restore(account, usage_complete=False)
    assert account.consumption_snapshot() == (70,0,False)
    with pytest.raises(ModelBudgetExceeded, match='usage_unknown'):
        account.reserve_model(task, input_tokens=1, max_output_tokens=1)


def test_exhausted_restored_account_is_not_refunded():
    task, account = fresh()
    restore(account, consumed_tokens=100)
    with pytest.raises(ModelBudgetExceeded, match='budget_exhausted'):
        account.reserve_model(task, input_tokens=1, max_output_tokens=1)
    assert account.consumption_snapshot() == (100,0,True)


@pytest.mark.parametrize('changes', [{'consumed_tokens':True}, {'consumed_tokens':-1},
    {'last_attempt_ordinal':True}, {'last_attempt_ordinal':-1},
    {'usage_complete':1}, {'root_task_id':'foreign-root'}])
def test_invalid_recovery_values_leave_pristine_account(changes):
    _, account = fresh()
    with pytest.raises(ValueError, match='invalid'):
        restore(account, **changes)
    assert account.consumption_snapshot() == (0,0,True)


def test_zero_history_restore_is_one_time_and_cannot_erase_prior_admission():
    task, account = fresh()
    restore(account, consumed_tokens=0, last_attempt_ordinal=0)
    with pytest.raises(ValueError, match='pristine'):
        restore(account)
    task, other = fresh()
    lease = other.reserve_model(task, input_tokens=1, max_output_tokens=1)
    lease.settle(unsent=True)
    with pytest.raises(ValueError, match='pristine'):
        restore(other)
