"""Recovery restores shared consumption once without multiplying or weakening limits."""
from copy import deepcopy
import threading

import pytest

from lib.agent_core.execution_budget import (
    _ensure_execution_budget, inherit_execution_budget, close_execution_budget,
)
from lib.orchestration.execution_journal import RecoveryRefused
from lib.orchestration.recovery_account import prepare_recovery_account

pytestmark = pytest.mark.unit


def root(name='source'):
    return {'id': name, '_userId': 1, 'status': 'running', 'abort_event': threading.Event(),
        'config': {'executionLimits': {'max_tokens': 40, 'max_tool_calls': 4, 'deadline': None}}}


def child(parent, name):
    task = dict(id=name, _userId=1, _parentTaskId=parent['id'], status='running', config={},
                abort_event=threading.Event())
    inherit_execution_budget(parent, task)
    return task


def spend(task, amount, tools=0):
    account = task['_executionBudget']
    lease = account.reserve_model(task, input_tokens=0, max_output_tokens=amount)
    account.mark_model_dispatched(lease.ordinal)
    lease.settle({'prompt_tokens': 0, 'completion_tokens': amount})
    for _ in range(tools):
        assert account.reserve_tool(task) is None


def receipt(task):
    account = task['_executionBudget']
    return {'kind': 'agent', 'invocation_id': task['id'],
        'invocation_completion': {'identity': {'user_id': 1, 'child_task_id': task['id']},
                                 'account_root_task_id': account.root_id},
        'execution_account': account.capture_replay_snapshot(task, owner=1)}


def test_parallel_samples_restore_one_total_and_idempotence_cannot_rewind():
    source = root()
    first, second = child(source, 'first'), child(source, 'second')
    spend(first, 10, 1); close_execution_budget(first)
    earlier = receipt(first)
    assert not earlier['execution_account']['complete']
    spend(second, 15, 2); close_execution_budget(second)
    later = receipt(second)
    assert later['execution_account']['complete']
    target = root('target')
    restore = prepare_recovery_account(target, {'earlier': earlier, 'later': later})
    restore(); restore()
    budget = target['_executionBudget']
    assert budget.consumption() == (25, 3)
    spend(target, 5, 1)
    restore()
    assert budget.consumption() == (30, 4)
    assert budget.reserve_tool(target) == 'execution_tool_budget_exhausted'
    lease = budget.reserve_model(target, input_tokens=0, max_output_tokens=40)
    assert lease.max_output_tokens == 10
    lease.settle(unsent=True)


def test_newer_unsettled_observation_cannot_fall_back_to_an_older_balance():
    source = root(); first = child(source, 'first')
    spend(first, 10); close_execution_budget(first)
    earlier = receipt(first)
    second = child(source, 'second')
    budget = source['_executionBudget']
    lease = budget.reserve_model(second, input_tokens=0, max_output_tokens=10)
    later = receipt(second)
    with pytest.raises(RecoveryRefused, match='settled consumption frontier'):
        prepare_recovery_account(root('target'), {'old': earlier, 'new': later})
    lease.settle(unsent=True); close_execution_budget(second)


@pytest.mark.parametrize('damage', ['owner', 'limits', 'root', 'unknown', 'boolean', 'missing'])
def test_changed_or_incomplete_evidence_cannot_grant_budget(damage):
    source = root(); actor = child(source, 'worker'); spend(actor, 15, 1)
    close_execution_budget(actor); record = receipt(actor); snapshot = record['execution_account']
    if damage == 'owner': snapshot['owner_user_id'] = 2
    elif damage == 'limits': snapshot['limits']['max_tokens'] = 400
    elif damage == 'root': snapshot['root_task_id'] = 'different-root'
    elif damage == 'unknown': snapshot['usage_known'] = False
    elif damage == 'boolean': snapshot['tokens'] = True
    elif damage == 'missing': record.pop('execution_account')
    target = root('target')
    with pytest.raises(RecoveryRefused):
        prepare_recovery_account(target, {'worker': record})
    assert target['_executionBudget'].consumption() == (0, 0)


def test_restore_refuses_an_account_that_already_admitted_work():
    source = root(); actor = child(source, 'worker'); spend(actor, 15, 1)
    close_execution_budget(actor); record = receipt(actor)
    target = root('target'); restore = prepare_recovery_account(target, {'worker': record})
    assert target['_executionBudget'].reserve_tool(target) is None
    with pytest.raises(RecoveryRefused, match='could not be restored'):
        restore()
    assert target['_executionBudget'].consumption() == (0, 1)


def test_unknown_zero_delta_does_not_become_known_by_choosing_an_older_sample():
    source = root(); actor = child(source, 'worker'); spend(actor, 15)
    close_execution_budget(actor); earlier = receipt(actor)
    later = deepcopy(earlier); later['execution_account']['usage_known'] = False
    later['execution_account']['complete'] = False
    with pytest.raises(RecoveryRefused, match='unknown execution consumption'):
        prepare_recovery_account(root('target'), {'old': earlier, 'new': later})


def test_snapshot_uses_the_existing_account_without_closing_a_live_reservation():
    source = root(); actor = child(source, 'worker')
    budget = _ensure_execution_budget(source)
    lease = budget.reserve_model(actor, input_tokens=0, max_output_tokens=20)
    snapshot = receipt(actor)['execution_account']
    assert not snapshot['settled'] and not snapshot['complete']
    assert snapshot['model_ordinal'] == 1
    assert budget.continuity_snapshot()['pending_model_requests'] == 1
    lease.settle(unsent=True); close_execution_budget(actor)


def test_second_generation_adds_only_new_spending_to_the_inherited_total():
    source = root(); first = child(source, 'first'); spend(first, 15, 1)
    close_execution_budget(first); original = receipt(first)
    successor = root('successor')
    prepare_recovery_account(successor, {'original': original})()
    second = child(successor, 'second'); spend(second, 10, 2)
    close_execution_budget(second); later = receipt(second)
    assert later['execution_account']['tokens'] == 25
    assert later['execution_account']['model_ordinal'] == 2
    target = root('target')
    prepare_recovery_account(target, {'original': original, 'later': later})()
    assert target['_executionBudget'].consumption() == (25, 3)
    assert target['_executionBudget'].actuals_complete()


@pytest.mark.parametrize('displayed', [0, 15])
def test_existing_checkpoint_display_is_not_counted_twice(displayed):
    source = root(); actor = child(source, 'worker'); spend(actor, 15, 1)
    close_execution_budget(actor); original = receipt(actor)
    target = root('target')
    target['usage'] = target['_checkpointUsage'] = {
        'prompt_tokens': 0, 'completion_tokens': displayed}
    prepare_recovery_account(target, {'original': original})()
    assert target['_executionBudget'].consumption() == (15, 1)


def test_new_unbounded_receipt_cannot_drop_its_account_evidence():
    source = root(); source['config']['executionLimits']['max_tokens'] = None
    source['config']['executionLimits']['max_tool_calls'] = None
    actor = child(source, 'worker'); close_execution_budget(actor)
    record = receipt(actor); record.pop('execution_account')
    with pytest.raises(RecoveryRefused, match='missing its execution account'):
        prepare_recovery_account(source, {'worker': record})


def test_detached_child_with_unknown_actuals_does_not_certify_a_complete_balance():
    source = root(); actor = child(source, 'worker'); spend(actor, 15)
    actor['_executionActualsComplete'] = False
    close_execution_budget(actor); record = receipt(actor)
    assert not record['execution_account']['complete']
    assert not record['execution_account']['usage_known']
    with pytest.raises(RecoveryRefused, match='unknown execution consumption'):
        prepare_recovery_account(root('target'), {'worker': record})
