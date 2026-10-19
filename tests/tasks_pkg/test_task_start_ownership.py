"""Fault-injection contracts for conversation task dispatch ownership.

No task may remain ``running`` unless either a durable attempt is bound to it
and a worker owns it, or the canonical terminal manager has settled it.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest


pytestmark = pytest.mark.unit


@pytest.fixture()
def task_start_fixture(monkeypatch):
    import lib.conversation_sync.task_start as task_start

    config = {
        'model': 'fixture-model',
        '_turnOwnerUserId': 1,
        '_turnId': 'turn-ownership',
        '_attemptId': 'attempt-ownership',
    }
    task = {
        'id': 'task-ownership',
        'convId': 'conv-ownership',
        'config': config,
        'status': 'running',
    }
    discarded = []

    monkeypatch.setattr(task_start, '_throttled_cleanup_old_tasks', lambda: None)
    monkeypatch.setattr(task_start, 'create_task', lambda *args, **kwargs: task)
    monkeypatch.setattr(
        'lib.turn_lifecycle.build_api_messages',
        lambda *args, **kwargs: [{'role': 'user', 'content': 'hello'}],
    )
    monkeypatch.setattr(
        'lib.tasks_pkg.manager.discard_task',
        lambda task_id, conv_id=None: discarded.append((task_id, conv_id)),
    )
    return task_start, config, task, discarded


def test_task_registration_receives_the_accepted_principal_tenant(monkeypatch,task_start_fixture):
    runtime,config,task,_=task_start_fixture
    config['_turnPrincipalTenantId']='accepted-tenant'
    captured=[]
    def register(*args,principal,user_id,**kwargs):
        captured.append(principal)
        assert principal.require_owner()==user_id==1
        return task
    monkeypatch.setattr(runtime,'create_task',register)
    def stop_before_dispatch(*args):
        raise RuntimeError('fixture stops before billable work')
    monkeypatch.setattr(runtime,'_prepare_unbound_task_dispatch',stop_before_dispatch)
    with pytest.raises(RuntimeError,match='fixture stops'):
        runtime.start_conversation_attempt_executor('conv-ownership',config)
    assert captured[0].tenant_id=='accepted-tenant'
    assert captured[0].scopes==frozenset()


def test_request_budget_binding_precedes_spawn_and_uses_existing_rollback(monkeypatch, task_start_fixture):
    runtime, config, task, discarded = task_start_fixture
    bound = []
    monkeypatch.setattr(runtime, '_prepare_unbound_task_dispatch', lambda *a: (config, None, False))
    def reject_budget(actual):
        assert actual is task and bound == [task['id']]
        raise RuntimeError('request allowance registration failed')
    monkeypatch.setattr('lib.project_dispatch.request_budget.bind_source_account', reject_budget)
    monkeypatch.setattr('lib.tasks_pkg.spawn.spawn_task', lambda *a, **kw: pytest.fail('Unbudgeted task started'))
    with pytest.raises(RuntimeError, match='request allowance'):
        runtime.start_conversation_attempt_executor('conv-ownership', config, on_task_registered=bound.append)
    assert discarded == [('task-ownership', 'conv-ownership')]


def test_prebind_preparation_exception_discards_registry_carrier(
    monkeypatch,
    task_start_fixture,
):
    task_start, config, _task, discarded = task_start_fixture
    bound = []

    def reject_dispatch(_config):
        raise RuntimeError('injected pre-bind dispatch failure')

    monkeypatch.setattr(
        'lib.orchestration.chat_flow_runner.resolve_chat_flow_entry',
        reject_dispatch,
    )

    with pytest.raises(RuntimeError, match='injected pre-bind dispatch failure'):
        task_start.start_conversation_attempt_executor(
            'conv-ownership',
            config,
            on_task_registered=bound.append,
        )

    assert bound == []
    assert discarded == [('task-ownership', 'conv-ownership')]


@pytest.mark.parametrize('from_request', [True, False])
def test_execution_origin_precedes_context_build_and_survives_registration(
    monkeypatch, task_start_fixture, from_request,
):
    from lib.agent_core import execution_clock as clocks
    task_start, config, task, discarded = task_start_fixture
    ticks = [10_000_000_000]
    monkeypatch.setattr(clocks.time, 'monotonic_ns', lambda: ticks[0])
    monkeypatch.setattr(clocks.time, 'time', lambda: 1000.0)
    origin = clocks.ExecutionClock.start() if from_request else None
    if from_request:
        ticks[0] += 250_000_000

    def build_context(*args, **kwargs):
        ticks[0] += 500_000_000
        return [{'role': 'user', 'content': 'hello'}]

    captured = []
    def register(*args, execution_clock, **kwargs):
        captured.append(execution_clock)
        return task

    def reject_preparation(*args):
        raise RuntimeError('injected preparation failure')

    monkeypatch.setattr('lib.turn_lifecycle.build_api_messages', build_context)
    monkeypatch.setattr(task_start, 'create_task', register)
    monkeypatch.setattr(task_start, '_prepare_unbound_task_dispatch', reject_preparation)
    with pytest.raises(RuntimeError, match='preparation failure'):
        task_start.start_conversation_attempt_executor(
            'conv-ownership', config, execution_clock=origin)
    clock = captured[0]
    assert clock.now_ms() - clock.origin_at_ms == (750 if from_request else 500)
    if from_request:
        assert clock is origin
    assert discarded == [('task-ownership', 'conv-ownership')]


@pytest.mark.parametrize('cancelled', [False, True])
def test_binding_exception_uses_same_unstarted_task_rollback(
    monkeypatch,
    task_start_fixture,
    cancelled,
):
    task_start, config, _task, discarded = task_start_fixture
    monkeypatch.setattr(
        'lib.orchestration.chat_flow_runner.resolve_chat_flow_entry',
        lambda _config: None,
    )

    def reject_binding(_task_id):
        if cancelled:
            from lib.conversation_sync.dispatch_contract import AttemptDispatchCancelled
            raise AttemptDispatchCancelled('injected bind failure')
        raise RuntimeError('injected bind failure')

    with pytest.raises(RuntimeError, match='injected bind failure'):
        task_start.start_conversation_attempt_executor(
            'conv-ownership',
            config,
            on_task_registered=reject_binding,
        )

    assert discarded == [('task-ownership', 'conv-ownership')]


def test_synchronous_worker_submission_failure_discards_bound_carrier(
    monkeypatch,
    task_start_fixture,
):
    task_start, config, _task, discarded = task_start_fixture
    bound = []
    monkeypatch.setattr(
        'lib.orchestration.chat_flow_runner.resolve_chat_flow_entry',
        lambda _config: None,
    )

    def reject_submission(_task):
        raise RuntimeError('injected synchronous submission failure')

    monkeypatch.setattr('lib.tasks_pkg.spawn.spawn_task', reject_submission)

    task_id, reason = task_start.start_conversation_attempt_executor(
        'conv-ownership',
        config,
        on_task_registered=bound.append,
    )

    assert (task_id, reason) == (None, 'executor_start_failed')
    assert bound == ['task-ownership']
    assert discarded == [('task-ownership', 'conv-ownership')]


def test_resume_model_switch_emits_fallback_after_bind_before_spawn(
    monkeypatch,
    task_start_fixture,
):
    task_start, config, task, discarded = task_start_fixture
    config['_resumeModelSwitchFrom'] = 'claude-opus-5'
    config['model'] = 'kimi-k3'
    bound = []
    emitted = []
    spawned = []

    monkeypatch.setattr(
        'lib.orchestration.chat_flow_runner.resolve_chat_flow_entry',
        lambda _config: None,
    )
    monkeypatch.setattr(
        'lib.tasks_pkg.manager.append_event',
        lambda _task, event: emitted.append(event),
    )
    monkeypatch.setattr(
        'lib.tasks_pkg.spawn.spawn_task',
        lambda _task, **kwargs: spawned.append(_task['id']),
    )

    task_id, reason = task_start.start_conversation_attempt_executor(
        'conv-ownership',
        config,
        on_task_registered=bound.append,
    )

    assert (task_id, reason) == ('task-ownership', None)
    assert bound == ['task-ownership']
    assert discarded == []
    # The event lands after the durable bind and before the worker spawns.
    assert [event['type'] for event in emitted] == ['model_fallback']
    event = emitted[0]
    assert event['fallbackFrom'] == 'claude-opus-5'
    assert event['fallbackModel'] == 'kimi-k3'
    assert event['fallbackKind'] == 'user_switch'
    assert task['_fallback_from'] == 'claude-opus-5'
    assert task['_fallback_model'] == 'kimi-k3'
    assert task['_fallback_kind'] == 'user_switch'
    assert spawned == ['task-ownership']


def test_no_switch_marker_emits_no_fallback_event(
    monkeypatch,
    task_start_fixture,
):
    task_start, config, task, _discarded = task_start_fixture
    emitted = []

    monkeypatch.setattr(
        'lib.orchestration.chat_flow_runner.resolve_chat_flow_entry',
        lambda _config: None,
    )
    monkeypatch.setattr(
        'lib.tasks_pkg.manager.append_event',
        lambda _task, event: emitted.append(event),
    )
    monkeypatch.setattr(
        'lib.tasks_pkg.spawn.spawn_task',
        lambda _task, **kwargs: None,
    )

    task_id, reason = task_start.start_conversation_attempt_executor(
        'conv-ownership',
        config,
        on_task_registered=lambda _task_id: None,
    )

    assert (task_id, reason) == ('task-ownership', None)
    assert emitted == []
    assert '_fallback_from' not in task

@pytest.fixture()
def clean_spawn_state():
    import lib.tasks_pkg.spawn as spawn

    spawn.set_serving_loop(None)
    spawn.set_agent_executor(None)
    yield spawn
    spawn.set_serving_loop(None)
    spawn.set_agent_executor(None)


def test_executor_rejection_uses_canonical_terminal_settlement(
    monkeypatch,
    clean_spawn_state,
):
    spawn = clean_spawn_state
    worker_calls = []
    terminal_events = []
    persisted = []
    notified = []

    monkeypatch.setattr(
        'lib.tasks_pkg.orchestrator.api.run_task',
        lambda task: worker_calls.append(task),
    )
    monkeypatch.setattr('lib.agent_core.events.emit_phase', lambda *args, **kwargs: None)

    from lib.tasks_pkg.manager._terminal import finalize_chat_task_error

    def settle_with_injected_boundaries(task, error, **kwargs):
        return finalize_chat_task_error(
            task,
            error,
            flow_reason=kwargs['flow_reason'],
            append_event_fn=lambda _task, event: terminal_events.append(event),
            persist_task_result_fn=persisted.append,
            notify_terminal_fn=notified.append,
        )

    monkeypatch.setattr(
        'lib.tasks_pkg.manager.finalize_chat_task_error',
        settle_with_injected_boundaries,
    )

    task = {
        'id': 'task-rejected',
        'convId': 'conv-rejected',
        'status': 'running',
        'config': {'model': 'fixture-model'},
    }
    rejected_pool = ThreadPoolExecutor(max_workers=1)
    rejected_pool.shutdown(wait=True)
    spawn.set_agent_executor(rejected_pool)

    async def drive_submission():
        spawn.spawn_task(task)
        for _ in range(200):
            if task.get('status') == 'error':
                return
            await asyncio.sleep(0.01)

    asyncio.run(drive_submission())

    assert worker_calls == []
    assert task['status'] == 'error'
    assert task['finishReason'] == 'error'
    assert task['_flow_stop_reason'] == 'executor_start_failed'
    assert task['error']['kind'] == 'task_start_failed'
    assert task['error']['retryable'] is True
    assert 'cannot schedule new futures after shutdown' in task['error']['raw']
    assert [event['type'] for event in terminal_events] == ['done']
    assert terminal_events[0]['finishReason'] == 'error'
    assert persisted == [task]
    assert notified == [task]


def test_failure_after_worker_entry_remains_worker_owned(
    monkeypatch,
    clean_spawn_state,
):
    spawn = clean_spawn_state
    rejected = []
    monkeypatch.setattr(
        spawn,
        '_finalize_rejected_submission',
        lambda task, error: rejected.append((task, error)),
    )

    def fail_inside_worker(_task):
        raise RuntimeError('injected worker-owned failure')

    task = {'id': 'task-worker-owned', 'status': 'running'}

    async def run_worker():
        loop = asyncio.get_running_loop()
        await spawn._executor_runner(loop, fail_inside_worker, task)()

    asyncio.run(run_worker())
    assert rejected == []
