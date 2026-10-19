"""Observation matches tree/local admission without spending authority."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import pytest

from lib.agent_core.execution_budget import (
    ExecutionBudget, ModelBudgetExceeded, close_execution_budget,
    execution_allowance_snapshot, inherit_execution_budget, model_budget_admission,
)
from lib.agent_core.execution_limits import admit_tool_execution
from lib.llm.llm_errors import AbortedError

pytestmark = pytest.mark.unit


def owner(tokens=100, tools=None, deadline=None, name='owned-task'):
    return {'id': name, '_userId': 7, 'usage': {}, 'abort_event': threading.Event(),
            'config': {'executionLimits': {'max_tokens': tokens,
                'max_tool_calls': tools, 'deadline': deadline}}}


def test_initial_observation_reuses_the_actual_admission_account():
    task = owner(tools=5)
    first = execution_allowance_snapshot(task)
    account = task['_executionBudget']
    assert first['tokens_available'] == 100 and first['tool_calls_available'] == 5
    assert first['actuals_complete'] and account.consumption() == (0, 0)
    assert account._model_leases == {}
    lease = model_budget_admission(task)(input_tokens=10, max_output_tokens=30)
    assert task['_executionBudget'] is account
    following = execution_allowance_snapshot(task)
    assert following['tokens_observed'] == 0 and following['tokens_reserved'] == 40
    assert following['tokens_available'] == 60 and not following['actuals_complete']
    lease.settle(unsent=True)
    assert execution_allowance_snapshot(task) == first


def test_tree_local_usage_and_pending_requests_share_one_atomic_projection():
    root = owner(tools=9);root['usage'] = {'prompt_tokens': 20}
    child = owner(name='child');inherit_execution_budget(root, child)
    child['config']['executionLimits']['max_tokens'] = 60
    child['config']['executionLimits']['max_tool_calls'] = 1
    child['usage'] = {'prompt_tokens': 10}
    account = root['_executionBudget']
    first = account.reserve_model(root, input_tokens=5, max_output_tokens=15)
    second = account.reserve_model(child, input_tokens=5, max_output_tokens=5)
    snapshot = execution_allowance_snapshot(child)
    assert (snapshot['tokens_observed'], snapshot['tokens_reserved'], snapshot['tokens_available']) == (30, 30, 40)
    assert snapshot['token_percent_available'] == 40
    assert snapshot['tokens_remaining'] == 50
    assert admit_tool_execution(child) is None
    snapshot = execution_allowance_snapshot(child)
    assert snapshot['tool_calls_available'] == snapshot['tool_percent_available'] == 0
    assert account.consumption() == (30, 1)
    first.settle(unsent=True);second.settle(unsent=True)
    close_execution_budget(child)
    assert execution_allowance_snapshot(root)['tokens_available'] == 70
    assert execution_allowance_snapshot(root)['actuals_complete']


def test_unknown_provider_usage_is_not_an_invented_remaining_balance():
    task = owner();account = ExecutionBudget(task);task['_executionBudget'] = account
    lease = account.reserve_model(task, input_tokens=10, max_output_tokens=20)
    lease.settle(None)
    snapshot = execution_allowance_snapshot(task)
    assert snapshot['token_bounded'] and not snapshot['usage_known']
    assert snapshot['tokens_available'] is snapshot['token_percent_available'] is snapshot['tokens_remaining'] is None
    assert not snapshot['actuals_complete'] and snapshot['tokens_reserved'] == 0
    with pytest.raises(AbortedError, match='execution_token_usage_unknown'):
        model_budget_admission(task)(input_tokens=1, max_output_tokens=1)


def test_local_incomplete_usage_also_withholds_a_finite_balance():
    root = owner();child = owner(name='child');inherit_execution_budget(root, child)
    child['_executionActualsComplete'] = False
    assert not execution_allowance_snapshot(root)['usage_known']
    assert execution_allowance_snapshot(child)['tokens_available'] is None


def test_earliest_deadline_and_exhausted_zero_keep_their_meaning(monkeypatch):
    import lib.agent_core.execution_budget as budgets
    monkeypatch.setattr(budgets, 'time', SimpleNamespace(time=lambda: 100.25))
    root = owner(deadline=150);child = owner(name='child');inherit_execution_budget(root, child)
    child['config']['executionLimits']['deadline'] = 110.5
    assert execution_allowance_snapshot(child)['seconds_left'] == 10
    child['config']['executionLimits']['deadline'] = 200
    assert execution_allowance_snapshot(child)['seconds_left'] == 49
    zero = execution_allowance_snapshot(owner(tokens=0, tools=0, deadline=0))
    assert zero['tokens_available'] == zero['tool_calls_available'] == zero['seconds_left'] == 0
    assert zero['token_percent_available'] == zero['tool_percent_available'] == 0


@pytest.mark.parametrize('task', [{'id': 'plain', 'config': {}}, owner(tokens=None)])
def test_unbounded_work_has_no_observation_or_new_account(task):
    assert execution_allowance_snapshot(task) is None
    assert '_executionBudget' not in task and '_executionBudgetLock' not in task


def test_tools_only_allowance_tracks_usage_without_a_token_cap():
    task = owner(tokens=None, tools=2)
    assert not execution_allowance_snapshot(task)['token_bounded']
    lease = model_budget_admission(task)(input_tokens=20, max_output_tokens=0)
    lease.settle({'prompt_tokens': 20, 'completion_tokens': 10})
    assert execution_allowance_snapshot(task)['tokens_observed'] == 30
    assert admit_tool_execution(task) is None
    assert execution_allowance_snapshot(task)['tool_calls_available'] == 1


def test_foreign_and_closed_identities_cannot_observe_an_account():
    root = owner();child = owner(name='child');inherit_execution_budget(root, child)
    foreign = owner(name='foreign');foreign['_executionBudget'] = root['_executionBudget']
    with pytest.raises(ModelBudgetExceeded, match='execution_owner_cancelled'):
        execution_allowance_snapshot(foreign)
    close_execution_budget(child)
    with pytest.raises(ModelBudgetExceeded): execution_allowance_snapshot(child)
    carrier = {**root, 'config': dict(root['config'])}
    assert execution_allowance_snapshot(carrier) == execution_allowance_snapshot(root)


def test_concurrent_settlement_never_mixes_consumption_and_reservations():
    task = owner(tokens=1000);execution_allowance_snapshot(task)
    account = task['_executionBudget'];gate = threading.Event()
    def work():
        gate.wait(2)
        for _ in range(100):
            lease = account.reserve_model(task, input_tokens=1, max_output_tokens=1)
            lease.settle({'prompt_tokens': 1, 'completion_tokens': 1})
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(work);gate.set()
        for _ in range(200):
            snapshot = execution_allowance_snapshot(task)
            assert snapshot['tokens_available'] + snapshot['tokens_observed'] + snapshot['tokens_reserved'] == 1000
        future.result(timeout=3)
    assert execution_allowance_snapshot(task)['tokens_available'] == 800


@pytest.mark.parametrize('limits', [{}, {'max_tokens': False, 'max_tool_calls': None, 'deadline': None}])
def test_malformed_policies_cannot_be_rendered_as_valid_allowances(limits):
    task = owner();task['config']['executionLimits'] = limits
    with pytest.raises(ValueError): execution_allowance_snapshot(task)
