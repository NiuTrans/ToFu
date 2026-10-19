"""Prepared graph leaves retain ordinary execution, rejection and cleanup."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import threading

import pytest

from lib.agent_core.execution_budget import _ensure_execution_budget
from lib.agent_core.execution_session import execution_session_for_task
from lib.orchestration.agent_runner import OrchestrationAgentRunnerConfig, OrchestrationInvocationRunner
from tests.orchestration.test_orchestration_agent_runner import _node, _parent

pytestmark = pytest.mark.unit


def runner(monkeypatch):
    parent = _parent()
    budget = _ensure_execution_budget(parent)
    calls = []
    def execute(task, *, policy):
        calls.append(task['id'])
        task.update(status='done', content='ordinary result', finishReason='stop', streamState='provider_finished')
    monkeypatch.setattr('lib.tasks_pkg.child_invocation.execute_invocation', execute)
    adapter = OrchestrationInvocationRunner(OrchestrationAgentRunnerConfig(
        parent_task=parent, model='chosen', model_routing_policy='selected'),
        emit=lambda event: None, abort_check=lambda: False)
    return adapter, budget, calls


def test_all_children_can_prepare_without_dispatch_then_use_the_same_run_owner(monkeypatch):
    adapter, budget, calls = runner(monkeypatch)
    with ExitStack() as scope:
        prepared = [scope.enter_context(adapter.prepare(_node(), 'original input', i)) for i in range(3)]
        assert not calls
        assert all(item.child['status'] == 'pending' for item in prepared)
        assert len({item.child['id'] for item in prepared}) == 3
        assert all(item.child['_executionBudget'] is budget for item in prepared)
        for item in prepared:
            assert item.run().output == 'ordinary result'
            with pytest.raises(ValueError, match='consumed'): item.run()
    assert calls == [item.child['id'] for item in prepared]
    assert budget.continuity_snapshot()['active_child_task_ids'] == []
    assert budget.consumption_snapshot() == (0, 0, True)
    with pytest.raises(ValueError, match='closed'): prepared[0].run()


@pytest.mark.parametrize('failure', [None, RuntimeError, KeyboardInterrupt])
def test_scope_exit_settles_unused_children_without_dispatch(monkeypatch, failure):
    adapter, budget, calls = runner(monkeypatch)
    prepared = []
    def scope():
        with ExitStack() as stack:
            for _ in range(2): prepared.append(stack.enter_context(adapter.prepare(_node(), 'input', 0)))
            if failure is not None: raise failure('preparation failed')
    if failure is None: scope()
    else:
        with pytest.raises(failure, match='preparation failed'): scope()
    assert not calls
    for item in prepared:
        assert execution_session_for_task(item.child).is_terminal
        assert item.child['status'] == 'error'
        assert item.child['_invocationPublicationClosed'] is True
        with pytest.raises(ValueError, match='closed'): item.run()
    assert budget.continuity_snapshot()['active_child_task_ids'] == []
    assert budget.consumption_snapshot() == (0, 0, True)


def test_prepared_durable_child_cannot_omit_its_admission(monkeypatch):
    adapter, budget, calls = runner(monkeypatch)
    with adapter.prepare(_node(), 'input', 0, durable_admission=True) as prepared:
        with pytest.raises(ValueError, match='admission'): prepared.run()
        assert not calls and prepared.child['status'] == 'pending'
        admitted = []
        prepared.run(before_dispatch=lambda: admitted.append(True))
    assert admitted == [True] and calls == [prepared.child['id']]


def test_rejection_during_child_creation_preserves_the_admission_error(monkeypatch):
    adapter, budget, calls = runner(monkeypatch)
    def reject(*args, event_sink, **kwargs):
        # The ordinary child owner may publish terminal rejection before its
        # carrier can be returned, for example when native route binding fails.
        event_sink({'type':'done','error':'native route admission refused'})
        raise ValueError('native route admission refused')
    monkeypatch.setattr('lib.tasks_pkg.child_invocation.create_child_invocation', reject)
    with pytest.raises(ValueError, match='native route admission refused'):
        with adapter.prepare(_node(), 'input', 0):
            pytest.fail('rejected child reached its execution scope')
    assert not calls and not budget.continuity_snapshot()['active_child_task_ids']


@pytest.mark.parametrize('stop_owner', ['runner', 'branch'])
def test_transferred_child_uses_actual_branch_events_and_stop(monkeypatch, stop_owner):
    adapter, budget, calls = runner(monkeypatch)
    stale_events, actual_events = [], []
    adapter._emit = stale_events.append
    stopped = {'runner': False, 'branch': False}
    recipient = OrchestrationInvocationRunner(adapter._config, emit=actual_events.append,
        abort_check=lambda: stopped['runner'])
    def execute(task, *, policy):
        calls.append(task['id'])
        assert not task['_invocationAbortCheck']()
        task['_invocationEventSink']({'type': 'delta', 'content': 'actual branch output'})
        stopped[stop_owner] = True
        assert task['_invocationAbortCheck']()
        task.update(status='aborted', content='stopped', finishReason='abort', streamState='aborted')
    monkeypatch.setattr('lib.tasks_pkg.child_invocation.execute_invocation', execute)
    with adapter.prepare(_node(), 'input', 0, transfer_context=True) as prepared:
        result = prepared.run(recipient_runner=recipient,
            runtime_abort_check=lambda: stopped['branch'])
        assert result.status == 'cancelled'
    assert calls == [prepared.child['id']]
    assert not stale_events and any(event['type'] == 'step_delta' for event in actual_events)
    assert not budget.continuity_snapshot()['active_child_task_ids']


def test_context_owner_transfer_requires_preparation_opt_in(monkeypatch):
    adapter, budget, calls = runner(monkeypatch)
    recipient = OrchestrationInvocationRunner(adapter._config, emit=lambda event: None,
        abort_check=lambda: False)
    with adapter.prepare(_node(), 'input', 0) as prepared:
        with pytest.raises(ValueError, match='execution owner'):
            prepared.run(recipient_runner=recipient)
    assert not calls and not budget.continuity_snapshot()['active_child_task_ids']


def test_prepared_transfer_rejects_mutated_shared_tool_configuration(monkeypatch):
    adapter, budget, calls = runner(monkeypatch)
    recipient = OrchestrationInvocationRunner(adapter._config, emit=lambda event: None,
        abort_check=lambda: False)
    with adapter.prepare(_node(), 'input', 0, transfer_context=True) as prepared:
        adapter._config.all_tools.append({'name': 'newly_added_tool'})
        with pytest.raises(ValueError, match='execution owner'):
            prepared.run(recipient_runner=recipient)
    assert not calls and not budget.continuity_snapshot()['active_child_task_ids']


def test_cleanup_failure_preserves_primary_error_and_still_closes_ingress_and_budget(monkeypatch):
    from lib.tasks_pkg import manager
    adapter, budget, calls = runner(monkeypatch)
    reject = manager.reject_unstarted_chat_task
    def lost_ack(*args, **kwargs):
        reject(*args, **kwargs)
        raise OSError('terminal rejection acknowledgement lost')
    monkeypatch.setattr(manager, 'reject_unstarted_chat_task', lost_ack)
    with pytest.raises(RuntimeError, match='original preparation failure') as raised:
        with adapter.prepare(_node(), 'input', 0) as prepared:
            raise RuntimeError('original preparation failure')
    assert not calls and prepared.child['status'] == 'error'
    assert prepared.child['_invocationPublicationClosed'] is True
    assert budget.continuity_snapshot()['active_child_task_ids'] == []
    assert any('cleanup failed' in note for note in raised.value.__notes__)


def test_concurrent_consumers_cannot_run_a_prepared_child_twice(monkeypatch):
    adapter, _, calls = runner(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def execute(task, *, policy):
        calls.append(task['id']); entered.set()
        assert release.wait(5)
        task.update(status='done', content='one result', finishReason='stop', streamState='provider_finished')
    monkeypatch.setattr('lib.tasks_pkg.child_invocation.execute_invocation', execute)
    with adapter.prepare(_node(), 'input', 0) as prepared, ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(prepared.run)
        try:
            assert entered.wait(5)
            with pytest.raises(ValueError, match='consumed'): pool.submit(prepared.run).result(5)
        finally: release.set()
        assert first.result(5).output == 'one result'
    assert calls == [prepared.child['id']]


@pytest.mark.parametrize('interrupt_wait', [False, True])
def test_scope_close_cancels_and_joins_an_entered_child(monkeypatch, interrupt_wait):
    adapter, _, calls = runner(monkeypatch)
    entered, cleaning, finish = threading.Event(), threading.Event(), threading.Event()
    def execute(task, *, policy):
        calls.append(task['id']); entered.set()
        assert task['abort_event'].wait(5)
        cleaning.set()
        assert finish.wait(5)
        task.update(status='aborted', content='stopped', finishReason='abort', streamState='aborted')
    monkeypatch.setattr('lib.tasks_pkg.child_invocation.execute_invocation', execute)
    if interrupt_wait:
        from types import SimpleNamespace
        def condition():
            value = threading.Condition()
            original = value.wait
            interrupted = False
            def wait():
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt('interrupted scope wait')
                return original()
            value.wait = wait
            return value
        monkeypatch.setattr('lib.orchestration.agent_runner.threading',
            SimpleNamespace(Condition=condition, get_ident=threading.get_ident))
    scope = adapter.prepare(_node(), 'input', 0)
    prepared = scope.__enter__()
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(prepared.run)
        try:
            assert entered.wait(5)
            closing = pool.submit(scope.__exit__, None, None, None)
            assert cleaning.wait(5) and not closing.done()
        finally: finish.set()
        if interrupt_wait:
            with pytest.raises(KeyboardInterrupt, match='interrupted scope wait'): closing.result(5)
        else:
            closing.result(5)
        running.result(5)
    assert calls == [prepared.child['id']]
    assert prepared.child['_invocationPublicationClosed'] is True
