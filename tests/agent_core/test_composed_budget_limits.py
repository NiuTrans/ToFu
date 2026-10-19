"""Optional quota composition observes physical admission without a second executor."""
import pytest
from lib.agent_core.execution_budget import ExecutionBudget, ModelBudgetExceeded

pytestmark = pytest.mark.unit


def account():
    task = {'id': 'root', 'status': 'running', 'config': {'executionLimits': {
        'max_tokens': 100, 'max_tool_calls': 10, 'deadline': None}}}
    return task, ExecutionBudget(task)


def test_composition_can_only_narrow_before_effects():
    task, budget = account()
    budget.narrow_limits({'max_tokens': 200, 'max_tool_calls': 2, 'deadline': None})
    assert budget.limits == {'max_tokens': 100, 'max_tool_calls': 2, 'deadline': None}
    assert task['config']['executionLimits']['max_tool_calls'] == 10
    assert budget.reserve_tool(task) is None
    with pytest.raises(ValueError, match='before effects'):
        budget.narrow_limits({'max_tokens': 1, 'max_tool_calls': 1, 'deadline': None})


def test_reserved_but_unentered_tools_are_observed_and_refused_slots_not_spent():
    task, budget = account(); events = []
    def observe(event):
        if event['reservation_ordinal'] > 1:
            raise ModelBudgetExceeded('execution_tool_budget_exhausted')
        events.append(event)
    budget.bind_model_attempt_observer(lambda event: None, tool_reservation_observer=observe)
    assert budget.reserve_tool(task) is None
    assert budget.reserve_tool(task) == 'execution_tool_budget_exhausted'
    assert budget.consumption() == (0, 1)
    assert events == [{'root_task_id': 'root', 'task_id': 'root', 'reservation_ordinal': 1, 'account_tool_calls': 1}]


def test_optional_remaining_allowance_clamps_provider_output_without_mutating_local_caps():
    task, budget = account()
    budget.bind_model_attempt_observer(lambda event: None, model_allowance_provider=lambda: 12)
    lease = budget.reserve_model(task, input_tokens=5, max_output_tokens=80)
    assert lease.max_output_tokens == 7 and budget.limits['max_tokens'] == 100
    lease.settle(unsent=True)


def test_composed_allowance_cannot_use_the_uncapped_output_sentinel():
    task = {'id': 'root', 'status': 'running'}
    budget = ExecutionBudget(task)
    budget.bind_model_attempt_observer(lambda event: None, model_allowance_provider=lambda: 0)
    with pytest.raises(ModelBudgetExceeded):
        budget.reserve_model(task, input_tokens=5, max_output_tokens=0)


def test_restored_tool_prefix_is_not_reported_again_to_new_generation_observer():
    from tests.agent_core.test_shared_account_checkpoint import fresh, capture, PROFILE
    task, budget = fresh()
    assert budget.reserve_tool(task) is None
    saved = capture(task, budget)
    task, resumed = fresh(); events = []
    resumed.bind_model_attempt_observer(lambda event: None, tool_reservation_observer=events.append)
    resumed.restore_shared_account(saved, owner=7, task=task, counter_policy=PROFILE)
    assert resumed.reserve_tool(task) is None
    assert resumed.consumption() == (0, 2)
    assert events == [{'root_task_id': 'root', 'task_id': 'root', 'reservation_ordinal': 1, 'account_tool_calls': 2}]
