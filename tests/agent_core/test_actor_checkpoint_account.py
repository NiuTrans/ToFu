"""Actor checkpoint evidence observes one participant without pausing its siblings."""
from copy import deepcopy

import pytest

from lib.agent_core.execution_budget import inherit_execution_budget
from tests.agent_core.test_shared_account_checkpoint import fresh, PROFILE

pytestmark = pytest.mark.unit


def child(root, name):
    task = dict(id=name, _userId=7, config={}, status='running')
    inherit_execution_budget(root, task)
    return task


def spend(account, task, tokens, calls=0):
    lease = account.reserve_model(task, input_tokens=0, max_output_tokens=tokens)
    lease.mark_dispatched(); lease.settle({'prompt_tokens': 0, 'completion_tokens': tokens})
    for _ in range(calls):
        assert account.reserve_tool(task) is None


def capture(account, task):
    return account.capture_actor_account(owner=7, task=task, counter_policy=PROFILE)


def test_parallel_actor_evidence_does_not_duplicate_shared_consumption():
    root, account = fresh()
    first, second = child(root, 'first'), child(root, 'second')
    spend(account, first, 10, 1); spend(account, second, 15, 2)
    before = account.continuity_snapshot()
    one, two = capture(account, first), capture(account, second)
    assert (one['tokens'], one['tool_calls']) == (10, 1)
    assert (two['tokens'], two['tool_calls']) == (15, 2)
    assert account.consumption() == (25, 3)
    assert one['root_task_id'] == two['root_task_id'] == root['id']
    assert one['actor_task_id'] == first['id'] and two['actor_task_id'] == second['id']
    assert first['_executionBudget'] is second['_executionBudget'] is account
    assert account.continuity_snapshot() == before and not account.actuals_complete()
    target, target_account = fresh()
    with pytest.raises(ValueError):
        target_account.restore_shared_account(one, owner=7, task=target, counter_policy=PROFILE)


def test_sibling_request_can_remain_live_while_settled_actor_is_captured():
    root, account = fresh(); first, second = child(root, 'first'), child(root, 'second')
    spend(account, first, 10)
    before = capture(account, first)
    pending = account.reserve_model(second, input_tokens=1, max_output_tokens=10)
    assert capture(account, first) == before
    assert account.continuity_snapshot()['pending_model_requests'] == 1
    with pytest.raises(ValueError, match='settled participant'):
        capture(account, second)
    pending.mark_dispatched(); pending.settle(None)
    assert capture(account, first)['complete'] is True
    assert capture(account, first) == before
    with pytest.raises(ValueError, match='settled participant'):
        capture(account, second)


@pytest.mark.parametrize('damage', ['root', 'copy', 'owner', 'detached', 'pending_tool', 'unknown'])
def test_actor_scope_requires_its_exact_owned_settled_participant(damage):
    root, account = fresh(); actor = child(root, 'actor'); spend(account, actor, 10)
    attempt = None
    if damage == 'root': actor = root
    elif damage == 'copy': actor = dict(actor)
    elif damage == 'owner': actor['_userId'] = 8
    elif damage == 'detached': account.close(actor)
    elif damage == 'unknown': actor['_executionActualsComplete'] = False
    elif damage == 'pending_tool':
        account._tool_observer = lambda event: None
        attempt = account.begin_tool_attempt(actor, 'call', 'lookup', 0)
    before = deepcopy(account.continuity_snapshot())
    with pytest.raises(ValueError, match='settled participant'):
        capture(account, actor)
    assert account.continuity_snapshot() == before
    if attempt is not None: account.finish_tool_attempt(attempt, phase='returned')
