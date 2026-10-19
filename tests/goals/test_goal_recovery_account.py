"""Recovered Flow evidence must retain spent limits before any new dispatch."""
import pytest
from copy import deepcopy
from threading import Event
from types import SimpleNamespace
import time

from tests.goals.test_goal_conversation_recovery import (
    recovery as recovery, semantic_authority as semantic_authority,
)
from lib.goal_runs.execution_journal import create_goal_execution_journal
from lib.goal_runs.recovery import resume_chat_goal, bind_goal_recovery_task
from lib.orchestration.engine import FlowExecutor
from lib.orchestration.execution_journal import RecoveryRefused
from lib.turn_lifecycle import get_attempt

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.unit


@pytest.mark.parametrize('unresolved', ['dispatched', 'unknown_usage'])
def test_unknown_root_consumption_refuses_before_successor_admission(recovery, unresolved):
    case = recovery
    create_goal_execution_journal(case.task, case.definition, case.launch, runs=case.runs)
    account = case.task['_executionBudget']
    lease = account.reserve_model(case.task, input_tokens=1, max_output_tokens=2)
    lease.mark_dispatched()
    if unresolved == 'unknown_usage': lease.settle(None)
    case.retire()
    with pytest.raises(RecoveryRefused, match='complete settled frontier'):
        resume_chat_goal(case.runs, case.task['_goalRunId'], owner_user_id=case.owner, ports=case.ports)
    assert not case.started


@pytest.mark.parametrize('recovery', [{'executionLimits': {
    'max_tokens': 20, 'max_tool_calls': 1, 'deadline': None}}], indirect=True)
@pytest.mark.parametrize('observation_fault', [None, 'before_bind', 'after_bind'])
def test_root_spending_without_a_child_receipt_survives_successor_binding_loss(recovery, monkeypatch, observation_fault):
    from lib.agent_core.execution_budget import _ensure_execution_budget
    from lib.agent_core.execution_limits import admit_tool_execution
    case = recovery
    create_goal_execution_journal(case.task, case.definition, case.launch, runs=case.runs)
    source = case.task['_executionBudget']
    lease = source.reserve_model(case.task, input_tokens=2, max_output_tokens=13)
    lease.mark_dispatched(); lease.settle({'prompt_tokens': 2, 'completion_tokens': 13})
    assert admit_tool_execution(case.task) is None
    case.retire()
    resume_chat_goal(case.runs, case.task['_goalRunId'], owner_user_id=case.owner, ports=case.ports)
    def bind(task):
        seed = bind_goal_recovery_task(task, runs=case.runs,
            conversation_repository=case.ports.conversation_repository, get_attempt=get_attempt)
        assert seed.records == {} and seed.account_snapshot['tokens'] == 15
        case.service.start(task, seed.definition)
        return seed
    task = case.started[-1]; seed = bind(task)
    if observation_fault:
        from lib.goal_runs import account_journal
        install = account_journal.install_account_journal
        class Crash(BaseException): pass
        def lost_bind(*args, **kwargs):
            if observation_fault == 'after_bind': install(*args, **kwargs)
            raise Crash()
        with monkeypatch.context() as fault:
            fault.setattr(account_journal, 'install_account_journal', lost_bind)
            with pytest.raises(Crash):
                create_goal_execution_journal(task, seed.definition, seed.launch(), runs=case.runs)
        interrupted = task['_goalRunId']; case.retire()
        resume_chat_goal(case.runs, interrupted, owner_user_id=case.owner, ports=case.ports)
        task = case.started[-1]; seed = bind(task)
    create_goal_execution_journal(task, seed.definition, seed.launch(), runs=case.runs)
    restored = _ensure_execution_budget(task)
    assert restored.consumption() == (15, 1)
    assert admit_tool_execution(task) == 'execution_tool_budget_exhausted'
    lease = restored.reserve_model(task, input_tokens=0, max_output_tokens=20)
    assert lease.max_output_tokens == 5
    lease.settle(unsent=True)


@pytest.mark.parametrize('recovery', [{'isolation': 'shared-context', 'executionLimits': {
    'max_tokens': 20, 'max_tool_calls': 1, 'deadline': None}}], indirect=True)
@pytest.mark.parametrize('restore_fault', [None, 'before_restore', 'after_restore'])
def test_completed_child_recovery_keeps_consumed_model_and_tool_allowance(recovery, monkeypatch, restore_fault):
    import lib.tasks_pkg.child_invocation as children
    from lib.agent_core.execution_budget import _ensure_execution_budget
    from lib.agent_core.execution_limits import admit_tool_execution
    case = recovery
    class Crash(BaseException):
        pass
    def execute(child, *, policy):
        account = child['_executionBudget']
        lease = account.reserve_model(child, input_tokens=2, max_output_tokens=13)
        account.mark_model_dispatched(lease.ordinal)
        usage = {'prompt_tokens': 2, 'completion_tokens': 13}
        lease.settle(usage)
        assert admit_tool_execution(child) is None
        child['messages'].append({'role': 'assistant', 'content': 'completed original work'})
        child.update(status='done', _messages_include_current_run=True, usage=usage,
            content='completed original work', finishReason='stop', streamState='provider_finished')
    monkeypatch.setattr(children, 'execute_invocation', execute)
    original = case.runs.project_event
    def publish(run_id, seq, event, **kwargs):
        if event.get('type') == 'execution_effect_completed':
            raise Crash()
        return original(run_id, seq, event, **kwargs)
    monkeypatch.setattr(case.runs, 'project_event', publish)
    journal = create_goal_execution_journal(case.task, case.definition, case.launch, runs=case.runs)
    with pytest.raises(Crash):
        FlowExecutor(case.definition, parent_task=case.task, model=case.launch.model,
            model_routing_policy='selected', worker_context_policy='conversation',
            execution_journal=journal).run(initial_context=case.launch.initial_context)
    monkeypatch.setattr(case.runs, 'project_event', original)
    assert case.task['_executionBudget'].consumption() == (15, 1)
    case.retire()
    resume_chat_goal(case.runs, case.task['_goalRunId'], owner_user_id=case.owner, ports=case.ports)
    task = case.started[0]
    seed = bind_goal_recovery_task(task, runs=case.runs,
        conversation_repository=case.ports.conversation_repository, get_attempt=get_attempt)
    case.service.start(task, seed.definition)
    if restore_fault:
        from lib.agent_core.execution_budget import ExecutionBudget
        restore = ExecutionBudget.restore_replay_snapshot
        def lost_handoff(account, *args, **kwargs):
            if restore_fault == 'after_restore':
                restore(account, *args, **kwargs)
            raise Crash()
        with monkeypatch.context() as fault:
            fault.setattr(ExecutionBudget, 'restore_replay_snapshot', lost_handoff)
            with pytest.raises(Crash):
                create_goal_execution_journal(task, seed.definition, seed.launch(), runs=case.runs)
        interrupted = task['_goalRunId']
        case.retire()
        resume_chat_goal(case.runs, interrupted, owner_user_id=case.owner, ports=case.ports)
        task = case.started[-1]
        seed = bind_goal_recovery_task(task, runs=case.runs,
            conversation_repository=case.ports.conversation_repository, get_attempt=get_attempt)
        case.service.start(task, seed.definition)
    create_goal_execution_journal(task, seed.definition, seed.launch(), runs=case.runs)
    account = _ensure_execution_budget(task)
    assert account.consumption() == (15, 1)
    assert account.limits == {'max_tokens': 20, 'max_tool_calls': 1, 'deadline': None}
    assert admit_tool_execution(task) == 'execution_tool_budget_exhausted'
    lease = account.reserve_model(task, input_tokens=0, max_output_tokens=20)
    assert lease.max_output_tokens == 5
    lease.settle(unsent=True)


@pytest.mark.parametrize('recovery', [
    {'executionLimits': {'max_tokens': 0, 'max_tool_calls': 0, 'deadline': None}},
    {'executionLimits': {'max_tokens': None, 'max_tool_calls': None, 'deadline': 0}},
    {'executionLimits': {'max_tokens': 100, 'max_tool_calls': 2, 'deadline': 4102444800}},
], indirect=True)
def test_recovery_keeps_exact_accepted_zero_limits_and_absolute_expiry(recovery, monkeypatch):
    from lib.tasks_pkg import invocation
    case = recovery
    expected = case.task['config']['executionLimits']
    if expected['deadline'] == 0:
        # Build the accepted history before its absolute expiry, then advance
        # control time below. Expired tasks may not publish new admissions.
        monkeypatch.setattr(invocation, 'time', SimpleNamespace(
            time=lambda: -1.0, monotonic=time.monotonic))
    create_goal_execution_journal(case.task, case.definition, case.launch, runs=case.runs)
    case.retire()
    resume_chat_goal(case.runs, case.task['_goalRunId'], owner_user_id=case.owner, ports=case.ports)
    task = case.started[0]
    expected = case.task['config']['executionLimits']
    assert task['config']['executionLimits'] == expected
    accepted = get_attempt(task['_attemptId'], user_id=case.owner, include_execution_config=True)
    assert accepted['config']['executionLimits'] == expected
    task['config']['executionLimits'] = deepcopy(expected)
    task['config']['executionLimits']['max_tokens'] = 100000
    with pytest.raises(RecoveryRefused, match='execution limits differ'):
        bind_goal_recovery_task(task, runs=case.runs,
            conversation_repository=case.ports.conversation_repository, get_attempt=get_attempt)
    assert '_goalRecoverySeed' not in task
    task['config']['executionLimits'] = deepcopy(expected)
    seed = bind_goal_recovery_task(task, runs=case.runs,
        conversation_repository=case.ports.conversation_repository, get_attempt=get_attempt)
    case.service.start(task, seed.definition)
    create_goal_execution_journal(task, seed.definition, seed.launch(), runs=case.runs)
    assert task['_executionBudget'].limits == expected
    monkeypatch.setattr(invocation, 'time', time)
    task['abort_event'] = Event()
    assert invocation.invocation_abort_requested(task) is (expected['deadline'] == 0)
    if expected['deadline'] == 0:
        assert task['_executionLimitReason'] == 'execution_deadline_exceeded'
