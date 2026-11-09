"""Swarm descendants of a durable conversation attempt borrow its live claim.

Every command crosses a real storage authority; no model is called. The root
runs through the claimed-attempt worker exactly as ordinary chat does, and the
swarm is launched through the real ``spawn_agents`` tool, so spawn, detached
lifetime, Stop and restart exercise production ownership end to end.
"""
import json
import threading
import time
import uuid

import pytest

from tests.conversation.test_durable_attempt_execution import claimed as claimed

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def _session(task, owner, specs=()):
    from lib.swarm.master import MasterOrchestrator
    return MasterOrchestrator(task_id=task['id'], conv_id=task['convId'], specs=list(specs),
        user_id=owner, model='controlled', parent_config=task.get('config') or {},
        parent_task=task, inbox_key=task['convId'])


def _child(parent):
    from lib.tasks_pkg.child_invocation import create_child_invocation
    return create_child_invocation(parent, model='controlled', tools=[], objective='probe',
        event_sink=lambda event: None, abort_check=lambda: False)


def _birth(factory, owner, child_id):
    record = factory().query('record.get', {
        'namespace': 'task_results', 'key': child_id, 'user_id': owner})
    return ((record or {}).get('value') or {}).get('child_invocation')


def _wait(predicate, what):
    deadline = time.monotonic() + 120
    while not predicate():
        assert time.monotonic() < deadline, f'timed out waiting for {what}'
        time.sleep(0.05)


def _install_root(monkeypatch, body):
    """Make the claimed root run ``body(task)`` and then settle as completed."""
    from lib import turn_lifecycle as turns
    from lib.conversation_sync import task_start
    from lib.tasks_pkg.manager import append_event, persist_task_result, stamp_chat_task_terminal
    monkeypatch.setattr(task_start, '_throttled_cleanup_old_tasks', lambda: None)
    monkeypatch.setattr(turns, 'build_api_messages',
                        lambda *a, **kw: [{'role': 'user', 'content': 'accepted input'}])

    def run(task):
        body(task)
        task['content'] = 'spawned'
        stamp_chat_task_terminal(task, status='done', finish_reason='stop')
        append_event(task, {'type': 'done', 'finishReason': 'stop'})
        assert persist_task_result(task)

    monkeypatch.setattr('lib.tasks_pkg.orchestrator.api.run_task', run)


def _run_root(claimed, monkeypatch, body):  # noqa: F811
    from lib.conversation_sync.worker_execution import execute_claimed_attempt
    _install_root(monkeypatch, body)
    return execute_claimed_attempt(claimed[0], claimed[0].claim.payload)


class _Workers:
    """Controls child execution; each child proves its authority once released."""

    def __init__(self, monkeypatch, *, held=1):
        from lib.llm.llm_errors import AbortedError
        from lib.tasks_pkg import child_invocation as children
        from lib.tasks_pkg.invocation import invocation_abort_requested
        from lib.tasks_pkg.invocation_effects import persist_child_effect
        from lib.tasks_pkg.worker_binding import admit_model_dispatch
        self.entered, self.proved, self.release = [], [], threading.Event()
        self.held = held

        def execute(child, *, policy):
            self.entered.append(child['id'])
            if len(self.entered) <= self.held:
                while not self.release.wait(0.05):
                    if invocation_abort_requested(child):
                        raise AbortedError('worker stopped')
            # The model boundary and a guarded child checkpoint, as a real
            # round would use them, both on the claim the child inherited.
            admit_model_dispatch(child, child['_conversationWorkerContext'])
            effect = {'operationId': uuid.uuid4().hex, 'scope': 'invocation',
                      'state': 'entered', 'recovery': 'reconcile'}
            row = {'toolName': 'controlled', 'toolCallId': 'call', 'roundNum': 1,
                   'executionEffect': effect}
            persist_child_effect(child, row)
            effect['state'] = 'returned'
            persist_child_effect(child, row)
            self.proved.append((child['id'], child['_conversationWorkerContext']))
            child.update(status='done', content='worker complete', finishReason='stop')

        monkeypatch.setattr(children, 'execute_invocation', execute)


def _spawn(task, agents):
    from lib.swarm.integration import execute_swarm_tool
    handle = json.loads(execute_swarm_tool('spawn_agents', {'agents': agents}, task,
                                           all_tools=[], model='controlled'))
    assert handle['status'] == 'async_launched', handle


@pytest.fixture
def queued(semantic_authority, monkeypatch):
    """``claimed`` without the claim: the production runner claims it."""
    from lib import storage, turn_lifecycle as turns
    from lib.conversation_sync.execution_identity import capture_execution_identity
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    factory, owner, _engine = semantic_authority
    monkeypatch.setattr(turns, '_turn_client', lambda **kw: factory(**kw))
    monkeypatch.setattr(storage, 'get_storage_client', lambda **kw: factory(**kw))
    identity = uuid.uuid4().hex
    principal = PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner)
    config = capture_execution_identity({'model': 'controlled'}, owner, principal)
    pair = turns.create_turn_pair(identity, command_id=identity,
        input_projection={'content': 'accepted input'}, config=config, user_id=owner,
        conversation_defaults={'allowCreate': True, 'title': 'Queued', 'settings': {}})
    turns.dispatch_attempt_to_worker(principal, pair['attempt']['attemptId'])
    task_id = 'conversation-attempt:' + pair['attempt']['attemptId']
    yield task_id, pair, factory, owner
    from lib.tasks_pkg.event_log import flush_pending
    flush_pending(task_id)
    chat_task_runtime.discard(task_id)


@pytest.fixture
def swarm_registry(semantic_authority):
    yield
    from lib.swarm.integration import _active_sessions, _remove_session
    from lib.tasks_pkg.manager import reopen_task_admission
    reopen_task_admission()  # A restart case may fail while admission is closed.
    for key in list(_active_sessions):
        session = _active_sessions.get(key)
        if session is not None and not session.is_terminated:
            session.abort()
            session.wait_terminated()
        _remove_session(key, preserve_durable=True)


def _turn_status(claimed):  # noqa: F811
    from lib import turn_lifecycle as turns
    _context, pair, _factory, owner = claimed
    return turns.get_turn(pair['turn']['conversationId'], pair['turn']['turnId'],
                          user_id=owner)['status']


def _claim_live(context):
    from lib.durable_worker import StorageWorkerJobStore
    state = StorageWorkerJobStore().claim_state(context.claim, now_ms=time.time_ns() // 1_000_000)
    return state.get('ok') is True


def _execute_in_thread(context):
    from lib.conversation_sync.worker_execution import execute_claimed_attempt
    outcomes = []
    thread = threading.Thread(target=lambda: outcomes.append(
        execute_claimed_attempt(context, context.claim.payload)), daemon=True)
    thread.start()
    return thread, outcomes


def test_session_proxy_births_children_under_the_root_claim(claimed, monkeypatch):  # noqa: F811
    from lib.tasks_pkg.child_invocation import close_unstarted_child_invocation
    context, pair, factory, owner = claimed
    births = []

    def spawn(task):
        session = _session(task, owner)
        assert session._parent_task_proxy['_conversationWorkerContext'] is context
        child = _child(session._parent_task_proxy)
        try:
            assert child['_conversationWorkerContext'] is context
            births.append((child['id'], _birth(factory, owner, child['id'])))
        finally:
            close_unstarted_child_invocation(child, reason='probe complete')

    assert _run_root(claimed, monkeypatch, spawn).status == 'succeeded'
    [(child_id, identity)] = births
    assert identity['parent_task_id'] == context.claim.task_id
    assert identity['child_task_id'] == child_id


def test_wave_assignment_borrows_only_its_own_tasks_claim(claimed, monkeypatch):  # noqa: F811
    from lib.storage.errors import StorageError
    from lib.swarm.master import _retained_assignment_proxy, bind_assignment_context
    from lib.swarm.protocol import SubTaskSpec
    from lib.tasks_pkg.child_invocation import close_unstarted_child_invocation
    context, pair, factory, owner = claimed
    seen = []

    def spawn(task):
        session = _session(task, owner)
        spec = SubTaskSpec(id='wave', role='writer', objective='probe')
        bind_assignment_context(spec, parent_task=task, parent_config=task['config'],
                                all_tools=[], model='controlled')
        live, _ = _retained_assignment_proxy(spec, session._parent_task_proxy)
        spec.retained_assignment_parent = None  # Restart: no live caller.
        hosted, _ = _retained_assignment_proxy(spec, session._parent_task_proxy)
        foreign_session = {**session._parent_task_proxy, 'id': 'conversation-attempt:other'}
        foreign_session.pop('_conversationWorkerContext')
        foreign, _ = _retained_assignment_proxy(spec, foreign_session)
        seen.append((live.get('_conversationWorkerContext'),
                     hosted.get('_conversationWorkerContext'),
                     '_conversationWorkerContext' in foreign))
        child = _child(hosted)
        close_unstarted_child_invocation(child, reason='probe complete')
        with pytest.raises(StorageError) as refused:
            _child(foreign)
        # Every authority names the fence that refused the birth.
        assert (refused.value.code, refused.value.message) == (
            'database_conflict', 'Durable attempt requires a worker claim')

    assert _run_root(claimed, monkeypatch, spawn).status == 'succeeded'
    assert seen == [(context, context, False)]


def test_detached_workers_keep_the_root_claim_after_the_turn_settles(
        claimed, monkeypatch, swarm_registry):  # noqa: F811
    context, pair, factory, owner = claimed
    workers = _Workers(monkeypatch)
    _install_root(monkeypatch, lambda task: _spawn(task, [
        {'id': 'first', 'role': 'writer', 'objective': 'hold until released'},
        # Admitted only after ``first`` completes, i.e. after the root settled.
        {'id': 'second', 'role': 'writer', 'objective': 'start late', 'depends_on': ['first']},
    ]))
    thread, outcomes = _execute_in_thread(context)
    _wait(lambda: _turn_status(claimed) == 'completed' and workers.entered, 'settled root')
    assert thread.is_alive() and _claim_live(context)
    workers.release.set()
    thread.join(120)
    assert not thread.is_alive()
    [outcome] = outcomes
    assert outcome.status == 'succeeded'
    assert [proof for _child_id, proof in workers.proved] == [context, context]
    for child_id, _proof in workers.proved:
        assert _birth(factory, owner, child_id)['parent_task_id'] == context.claim.task_id


def test_stop_of_the_held_job_stops_its_detached_worker(claimed, monkeypatch, swarm_registry):  # noqa: F811
    from lib.durable_worker import StorageWorkerJobStore
    from tests.support.worker_job_time import wall_ms
    context, pair, factory, owner = claimed
    workers = _Workers(monkeypatch)
    _install_root(monkeypatch, lambda task: _spawn(task, [
        {'id': 'held', 'role': 'writer', 'objective': 'hold until stopped'}]))
    thread, outcomes = _execute_in_thread(context)
    _wait(lambda: _turn_status(claimed) == 'completed' and workers.entered, 'settled root')
    assert thread.is_alive()
    factory(write=True).command('worker_job.request_cancel', {
        'user_id': owner, 'task_id': context.claim.task_id, 'now_ms': wall_ms(),
        'reason': 'Stop'}, uuid.uuid4().hex)
    # What the job's heartbeat monitor observes on its next beat.
    context.observe_claim_state(StorageWorkerJobStore().claim_state(
        context.claim, now_ms=wall_ms()))
    thread.join(120)
    assert not thread.is_alive() and len(outcomes) == 1
    assert workers.proved == []


def test_runner_settles_the_held_job_when_it_is_stopped(queued, monkeypatch, swarm_registry):
    from lib.conversation_sync.worker_execution import conversation_registration
    from lib.durable_worker import StorageWorkerJobStore, SynchronousWorkerRunner
    from lib.storage.generated_worker_job_contract import WORKER_TERMINAL_STATES
    from tests.support.worker_job_time import wall_ms
    task_id, pair, factory, owner = queued
    workers = _Workers(monkeypatch)
    _install_root(monkeypatch, lambda task: _spawn(task, [
        {'id': 'held', 'role': 'writer', 'objective': 'hold until stopped'}]))
    runner = SynchronousWorkerRunner(worker_id='runner:' + uuid.uuid4().hex,
        store=StorageWorkerJobStore(), registrations=[conversation_registration()],
        lease_ms=30_000, heartbeat_interval_ms=100)
    results = []
    thread = threading.Thread(target=lambda: results.append(runner.run_once()), daemon=True)
    thread.start()
    _wait(lambda: _turn_status(queued) == 'completed' and workers.entered, 'settled root')

    def job():
        return factory().query('worker_job.get', {'task_id': task_id, 'user_id': owner})

    assert thread.is_alive() and job()['status'] == 'running'
    factory(write=True).command('worker_job.request_cancel', {
        'user_id': owner, 'task_id': task_id, 'now_ms': wall_ms(), 'reason': 'Stop'},
        uuid.uuid4().hex)
    # The runner's own heartbeat observes the Stop; nothing here relays it.
    thread.join(120)
    assert not thread.is_alive() and workers.proved == []
    [result] = results
    assert job()['status'] in WORKER_TERMINAL_STATES
    assert result.state == job()['status']


@pytest.mark.parametrize('shutdown_before_hold', [False, True],
                         ids=['shutdown-during-hold', 'shutdown-before-hold'])
def test_restart_defers_the_claim_and_takeover_resumes_the_worker(
        claimed, monkeypatch, swarm_registry, shutdown_before_hold):  # noqa: F811
    from lib.durable_worker import (
        StorageWorkerJobStore, WorkerExecutionContext, WorkerJobClaim, WorkerJobSuspension)
    from lib.swarm import persistence, turn_handoff
    from lib.swarm.integration import _remove_session, get_active_session
    from lib.tasks_pkg.manager import quiesce_running_tasks, reopen_task_admission
    from tests.support.worker_job_time import wall_ms
    original, pair, factory, owner = claimed
    conversation = pair['turn']['conversationId']
    shutdown = threading.Event()
    context = WorkerExecutionContext(original.claim, shutdown_event=shutdown)
    workers = _Workers(monkeypatch)
    _install_root(monkeypatch, lambda task: _spawn(task, [
        {'id': 'resumed', 'role': 'writer', 'objective': 'survive a restart'}]))
    if shutdown_before_hold:
        # Shutdown suspends the session after the root Turn settled but
        # before the attempt's hold first looks for live work.
        hold = turn_handoff.hold_detached_swarm

        def late_hold(context):
            # Only the pre-restart owner races shutdown; the takeover holds normally.
            monkeypatch.setattr(turn_handoff, 'hold_detached_swarm', hold)
            _wait(lambda: workers.entered, 'running worker')
            quiesce_running_tasks('server_shutdown')
            _wait(lambda: get_active_session(conversation).is_terminated, 'suspended session')
            hold(context)

        monkeypatch.setattr(turn_handoff, 'hold_detached_swarm', late_hold)
    thread, outcomes = _execute_in_thread(context)
    _wait(lambda: _turn_status(claimed) == 'completed' and workers.entered, 'settled root')

    # Production shutdown order: quiesce closes admission and suspends
    # sessions; the worker service stops (shutdown event) only later.
    try:
        if not shutdown_before_hold:
            quiesce_running_tasks('server_shutdown')
        _wait(lambda: get_active_session(conversation).is_terminated, 'suspended session')
        time.sleep(0.2)
        assert thread.is_alive(), 'a restart-suspended session must not release its claim'
        assert persistence.load_session(conversation, required=True)['status'] == 'running'
        shutdown.set()
        thread.join(120)
    finally:
        reopen_task_admission()
    assert not thread.is_alive() and workers.proved == []
    # The runner defers a claim whose handler returned during shutdown.
    store = StorageWorkerJobStore()
    assert store.defer(context.claim, now_ms=wall_ms(), outcome=WorkerJobSuspension(
        available_at_ms=wall_ms()), observed_cancel_sequence=0)['ok'] is True
    _remove_session(conversation, preserve_durable=True)  # The old process is gone.

    worker_id = uuid.uuid4().hex
    document = factory(write=True).command('worker_job.claim_next', {
        'worker_id': worker_id, 'task_kinds': ['conversation-attempt'],
        'now_ms': wall_ms(), 'lease_ms': 300_000}, uuid.uuid4().hex)
    takeover = WorkerExecutionContext(WorkerJobClaim.from_document(document, expected_worker_id=worker_id))
    assert takeover.claim.task_id == context.claim.task_id
    assert takeover.fencing_token > context.fencing_token
    workers.release.set()
    resumed_thread, resumed = _execute_in_thread(takeover)
    resumed_thread.join(120)
    assert not resumed_thread.is_alive()
    assert [outcome.status for outcome in resumed] == ['succeeded']
    [(child_id, proof)] = workers.proved
    assert proof is takeover
    assert _birth(factory, owner, child_id)['parent_task_id'] == context.claim.task_id
    assert persistence.load_session(conversation, required=True)['status'] == 'terminated'


def test_startup_defers_durable_detached_work_only_while_its_job_is_unfinished(claimed):  # noqa: F811
    from lib.durable_worker import StorageWorkerJobStore, WorkerJobOutcome
    from lib.swarm.integration import _rehydrate
    from tests.support.worker_job_time import wall_ms
    context, pair, factory, owner = claimed
    session = {'swarm_key': 'conversation', 'conv_id': 'conversation',
               'task_id': context.claim.task_id, 'status': 'running',
               'specs': [{'id': 'worker', 'role': 'writer', 'objective': 'probe'}],
               'config': {'user_id': owner, 'parent_cfg': {
                   '_swarmParentTaskId': context.claim.task_id}}}
    assert _rehydrate._owning_job_unfinished(context.claim.task_id, user_id=owner)
    assert _rehydrate._rehydrate_one_locked(session) is False
    # Once the job is finished no claim can resume the session, so startup
    # recovers it (undelivered results need no birth).
    assert StorageWorkerJobStore().complete(context.claim, now_ms=wall_ms(), outcome=WorkerJobOutcome(
        status='succeeded', result_ref=context.claim.task_id))['ok'] is True
    assert not _rehydrate._owning_job_unfinished(context.claim.task_id, user_id=owner)
    assert not _rehydrate._owning_job_unfinished('conversation-attempt:absent', user_id=owner)
