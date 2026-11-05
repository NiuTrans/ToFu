"""Restored tool ordinals continue durable observer sequences without granting slots."""
import pytest
from lib.agent_core.execution_budget import ExecutionBudget
pytestmark = pytest.mark.unit


def observed(cap=None):
    task = {'id': 'restored-root', 'status': 'running', 'config': {'executionLimits': {
        'max_tokens': None, 'max_tool_calls': cap, 'deadline': None}}}
    account = ExecutionBudget(task)
    tools, reservations = [], []
    account.bind_model_attempt_observer(lambda event: None, tool_observer=tools.append,
                                        tool_reservation_observer=reservations.append)
    return task, account, tools, reservations


def restore(account, **changes):
    values = dict(root_task_id='restored-root', last_tool_ordinal=5, last_reservation_ordinal=3)
    account.restore_tool_account(**{**values, **changes})


def test_restored_ordinals_continue_both_durable_sequences_without_spending_slots():
    task, account, tools, reservations = observed()
    restore(account)
    assert account.consumption() == (0, 0)
    assert account.reserve_tool(task) is None
    event = account.begin_tool_attempt(task, 'call', 'read_files', 1)
    account.finish_tool_attempt(event, phase='executor_returned')
    assert account.reserve_tool(task) is None
    assert [row['reservation_ordinal'] for row in reservations] == [4, 5]
    assert [(row['tool_ordinal'], row['phase']) for row in tools] == [(6, 'intent'), (6, 'executor_returned')]
    assert account.consumption() == (0, 2)


@pytest.mark.parametrize('changes', [{'last_tool_ordinal': True}, {'last_tool_ordinal': -1},
    {'last_reservation_ordinal': 1.0}, {'last_reservation_ordinal': -1}, {'root_task_id': 'foreign-root'}])
def test_invalid_recovery_values_leave_pristine_account(changes):
    task, account, _, reservations = observed()
    with pytest.raises(ValueError, match='invalid'):
        restore(account, **changes)
    assert account.reserve_tool(task) is None
    assert [row['reservation_ordinal'] for row in reservations] == [1]


def test_restore_is_one_time_and_cannot_rewind_admitted_tools():
    task, account, _, reservations = observed()
    restore(account, last_tool_ordinal=0, last_reservation_ordinal=0)
    with pytest.raises(ValueError, match='pristine'):
        restore(account)
    task, other, _, reservations = observed()
    assert other.reserve_tool(task) is None
    with pytest.raises(ValueError, match='pristine'):
        restore(other, last_reservation_ordinal=0)
    assert other.reserve_tool(task) is None
    assert [row['reservation_ordinal'] for row in reservations] == [1, 2]


def test_unobserved_sequence_or_finite_tool_cap_refuses_recovery():
    task = {'id': 'restored-root', 'status': 'running'}
    unobserved = ExecutionBudget(task)
    with pytest.raises(ValueError, match='pristine'):
        restore(unobserved)
    restore(unobserved, last_tool_ordinal=0, last_reservation_ordinal=0)
    # Spent slots are not restored, so a finite cap would be replenished.
    _, capped, _, _ = observed(cap=10)
    with pytest.raises(ValueError, match='pristine'):
        restore(capped)
    _, restored, _, _ = observed()
    restore(restored)
    with pytest.raises(ValueError, match='before effects'):
        restored.narrow_limits({'max_tokens': None, 'max_tool_calls': 10, 'deadline': None})
