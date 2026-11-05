"""Claimed chat uses real admission, task construction, publication and settlement.

The provider outcome is controlled; these tests make no paid/model call. All
ownership, Turn, replay and task-result commands cross real storage processes.
"""
import threading
import uuid

import pytest

from lib.durable_worker import WorkerDomainSettlementUnknown, WorkerExecutionContext, WorkerJobClaim
from lib.identity import PrincipalContext

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


@pytest.fixture
def claimed(semantic_authority, monkeypatch):
    from lib import storage, turn_lifecycle as turns
    from lib.conversation_sync.execution_identity import capture_execution_identity
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    factory, owner, _engine = semantic_authority
    monkeypatch.setattr(turns, '_turn_client', lambda **kw: factory(**kw))
    monkeypatch.setattr(storage, 'get_storage_client', lambda **kw: factory(**kw))
    identity = uuid.uuid4().hex
    principal = PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner)
    config = capture_execution_identity({'model': 'controlled'}, owner, principal)
    pair = turns.create_turn_pair(identity, command_id=identity,
        input_projection={'content': 'accepted input'}, config=config, user_id=owner,
        conversation_defaults={'allowCreate': True, 'title': 'Claimed', 'settings': {}})
    turns.dispatch_attempt_to_worker(principal, pair['attempt']['attemptId'])
    document = factory(write=True).command('worker_job.claim_next', {
        'worker_id': identity, 'task_kinds': ['conversation-attempt'],
        'now_ms': 1, 'lease_ms': 300_000}, uuid.uuid4().hex)
    context = WorkerExecutionContext(WorkerJobClaim.from_document(document, expected_worker_id=identity))
    yield context, pair, factory, owner
    from lib.tasks_pkg.event_log import flush_pending
    flush_pending(context.claim.task_id)
    chat_task_runtime.discard(context.claim.task_id)


@pytest.mark.parametrize('process_available', [True, False])
def test_claim_rebuilds_and_joins_common_chat_before_returning_outcome(
        claimed, monkeypatch, process_available):
    from lib import turn_lifecycle as turns
    from lib.conversation_sync import task_start
    from lib.conversation_sync.worker_execution import execute_claimed_attempt
    from lib.tasks_pkg.manager import append_event, persist_task_result, stamp_chat_task_terminal
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    context, pair, factory, owner = claimed
    if not process_available:
        monkeypatch.setattr('lib.process_owner.process_owner', lambda: None)
    caller = threading.get_ident()
    entered = []
    monkeypatch.setattr(task_start, '_throttled_cleanup_old_tasks', lambda: None)
    monkeypatch.setattr(turns, 'build_api_messages',
                        lambda *a, **kw: [{'role': 'user', 'content': 'accepted input'}])

    def run(task):
        assert threading.get_ident() == caller
        assert task['id'] == context.claim.task_id
        assert task['_conversationWorkerContext'] is context
        assert task['config']['model'] == 'controlled'
        from lib.process_owner import process_owner
        from lib.storage.generated_worker_job_contract import WORKER_EXECUTION_CONFIG_KEY
        accepted = turns.get_attempt(pair['attempt']['attemptId'], user_id=owner,
                                     include_execution_config=True)
        assert accepted['config'][WORKER_EXECUTION_CONFIG_KEY]['process'] == process_owner()
        assert turns.get_attempt(pair['attempt']['attemptId'], user_id=owner)['status'] == 'running'
        entered.append(task['id'])
        task['content'] = 'completed by the shared executor'
        append_event(task, {'type': 'delta', 'content': task['content']})
        stamp_chat_task_terminal(task, status='done', finish_reason='stop')
        append_event(task, {'type': 'done', 'finishReason': 'stop'})
        assert persist_task_result(task)

    monkeypatch.setattr('lib.tasks_pkg.orchestrator.api.run_task', run)
    outcome = execute_claimed_attempt(context, context.claim.payload)
    assert outcome.status == 'succeeded'
    task = chat_task_runtime.get(context.claim.task_id)
    assert not task.get('_terminalPersistencePending')
    assert task['_invocationAbortCheck'].observer_count == 0
    assert entered == [context.claim.task_id]
    assert turns.get_turn(pair['turn']['conversationId'], pair['turn']['turnId'],
                          user_id=owner)['status'] == 'completed'
    # Completion loss survives carrier eviction/process replacement as well.
    chat_task_runtime.discard(context.claim.task_id)
    assert execute_claimed_attempt(context, context.claim.payload) == outcome
    assert entered == [context.claim.task_id]


def test_entered_attempt_is_never_replayed_from_lease_ownership_alone(claimed, monkeypatch):
    from lib import turn_lifecycle as turns
    from lib.conversation_sync.worker_execution import execute_claimed_attempt
    context, pair, _factory, owner = claimed
    turns.mark_task_started(pair['attempt']['attemptId'], context.claim.task_id,
                           user_id=owner, worker_claim=context.publication_claim())
    monkeypatch.setattr('lib.conversation_sync.task_start.start_conversation_attempt_executor',
                        lambda *a, **kw: pytest.fail('entered effects were replayed'))
    with pytest.raises(WorkerDomainSettlementUnknown, match='recovery'):
        execute_claimed_attempt(context, context.claim.payload)


def test_old_storage_peer_cannot_silently_omit_process_entry(claimed, monkeypatch):
    from lib import turn_lifecycle as turns
    from lib.conversation_sync.worker_execution import execute_claimed_attempt
    from lib.durable_worker import WorkerProtocolError
    context, _pair, _factory, _owner = claimed
    original = turns.mark_task_started
    def old_peer(*args, **kwargs):
        kwargs.pop('worker_process', None)
        return original(*args, **kwargs)
    monkeypatch.setattr(turns, 'mark_task_started', old_peer)
    monkeypatch.setattr('lib.conversation_sync.task_start.start_conversation_attempt_executor',
                        lambda *a, **kw: pytest.fail('unacknowledged process entered preparation'))
    with pytest.raises(WorkerProtocolError, match='process entry'):
        execute_claimed_attempt(context, context.claim.payload)


def test_claim_reaches_flow_completion_and_nested_tool_admission(claimed, monkeypatch):
    import json
    from lib import turn_lifecycle as turns
    from lib.conversation_sync import task_start
    from lib.conversation_sync.worker_execution import execute_claimed_attempt
    from lib.orchestration.engine import FlowExecutor
    from lib.orchestration.execution_journal import ExecutionJournal
    from lib.tasks_pkg import child_invocation as children
    from lib.tasks_pkg.invocation_effects import persist_child_effect
    from lib.tasks_pkg.manager import append_event, discard_task, persist_task_result, stamp_chat_task_terminal
    from tests.orchestration.test_orchestration_execution_recovery import graph, role
    context, pair, factory, owner = claimed
    seen, events = [], []
    monkeypatch.setattr(task_start, '_throttled_cleanup_old_tasks', lambda: None)
    monkeypatch.setattr(turns, 'build_api_messages',
                        lambda *a, **kw: [{'role': 'user', 'content': 'accepted input'}])

    def execute(child, *, policy):
        assert child['_conversationWorkerContext'] is context
        seen.append(child['id'])
        nested = children.create_child_invocation(child, model='controlled', tools=[],
            objective='nested', event_sink=lambda event: None, abort_check=lambda: False)
        try:
            assert nested['_conversationWorkerContext'] is context
            effect = {'operationId': uuid.uuid4().hex, 'scope': 'invocation',
                      'state': 'entered', 'recovery': 'reconcile'}
            row = {'toolName': 'controlled', 'toolCallId': 'call', 'roundNum': 1,
                   'executionEffect': effect}
            persist_child_effect(nested, row)
            effect['state'] = 'returned'
            persist_child_effect(nested, row)
            record = factory().query('record.get', {
                'namespace': 'task_results', 'key': nested['id'], 'user_id': owner})
            assert json.loads(record['value']['tool_rounds'])[0]['executionEffect']['state'] == 'returned'
        finally:
            discard_task(nested['id'])
        child.update(status='done', content='child complete', finishReason='stop')

    def run(task):
        definition = graph(role('worker'))
        journal = ExecutionJournal(events.append)
        journal.begin(definition, 'input')
        FlowExecutor(definition, parent_task=task, model='controlled',
            model_routing_policy='selected', execution_journal=journal).run(initial_context='input')
        task['content'] = 'complete'
        stamp_chat_task_terminal(task, status='done', finish_reason='stop')
        append_event(task, {'type': 'done', 'finishReason': 'stop'})
        assert persist_task_result(task)

    monkeypatch.setattr(children, 'execute_invocation', execute)
    monkeypatch.setattr('lib.tasks_pkg.orchestrator.api.run_task', run)
    assert execute_claimed_attempt(context, context.claim.payload).status == 'succeeded'
    assert len(seen) == 1
    assert any(event['type'] == 'execution_effect_completed' for event in events)
    record = factory().query('record.get', {'namespace': 'task_results', 'key': seen[0], 'user_id': owner})
    assert 'flow_invocation_completion' in record['value']


@pytest.mark.parametrize('partial_carrier', [False, True])
def test_preparation_effects_follow_durable_entry_and_failure_is_not_replayed(
        claimed, monkeypatch, partial_carrier):
    from lib import turn_lifecycle as turns
    from lib.conversation_sync.worker_execution import execute_claimed_attempt
    context, pair, _factory, owner = claimed
    entered = []

    def fail(*args, **kw):
        assert turns.get_attempt(pair['attempt']['attemptId'], user_id=owner)['status'] == 'running'
        entered.append('preparation')
        if partial_carrier:
            from lib.tasks_pkg.manager.runtime import chat_task_runtime
            chat_task_runtime.create(principal=context.principal, task_id=context.claim.task_id)
        raise RuntimeError('unknown preparation outcome')

    monkeypatch.setattr('lib.conversation_sync.task_start.start_conversation_attempt_executor', fail)
    with pytest.raises(RuntimeError, match='unknown preparation'):
        execute_claimed_attempt(context, context.claim.payload)
    assert execute_claimed_attempt(context, context.claim.payload).status == 'failed'
    assert entered == ['preparation']


@pytest.mark.parametrize('internal', [False, True])
def test_running_stop_uses_common_cancellation_and_terminal_receipts(claimed, monkeypatch, internal):
    from lib import turn_lifecycle as turns
    from lib.conversation_sync import task_start
    from lib.conversation_sync.worker_execution import execute_claimed_attempt
    from lib.tasks_pkg.manager import finalize_chat_task_aborted
    from lib.tasks_pkg.invocation import invocation_abort_requested
    context, pair, factory, owner = claimed
    monkeypatch.setattr(task_start, '_throttled_cleanup_old_tasks', lambda: None)
    monkeypatch.setattr(turns, 'build_api_messages',
                        lambda *a, **kw: [{'role': 'user', 'content': 'accepted input'}])

    def run(task):
        from lib.conversation_sync.worker_control import encode_cancellation
        reason = 'dispatch_authority_lost' if internal else ''
        detail = 'Coordinator lease was revoked' if internal else ''
        factory(write=True).command('worker_job.request_cancel', {
            'task_id': context.claim.task_id, 'user_id': owner, 'now_ms': 1,
            'reason': encode_cancellation(reason, detail),
        }, uuid.uuid4().hex)
        from lib.durable_worker import StorageWorkerJobStore
        context.observe_claim_state(StorageWorkerJobStore(client_factory=factory).claim_state(
            context.claim, now_ms=1))
        assert invocation_abort_requested(task)
        assert task.get('_abort_reason', '') == reason
        assert task.get('_abortReasonDetail', '') == detail
        finalize_chat_task_aborted(task)

    monkeypatch.setattr('lib.tasks_pkg.orchestrator.api.run_task', run)
    assert execute_claimed_attempt(context, context.claim.payload).status == 'cancelled'
    assert turns.get_attempt(pair['attempt']['attemptId'], user_id=owner)['status'] == (
        'failed' if internal else 'interrupted')


def test_terminal_persistence_debt_cannot_complete_worker_job(claimed, monkeypatch):
    from lib import turn_lifecycle as turns
    from lib.conversation_sync import task_start
    from lib.conversation_sync.worker_execution import execute_claimed_attempt
    from lib.tasks_pkg.manager import append_event, stamp_chat_task_terminal
    context, _pair, _factory, _owner = claimed
    monkeypatch.setattr(task_start, '_throttled_cleanup_old_tasks', lambda: None)
    monkeypatch.setattr(turns, 'build_api_messages',
                        lambda *a, **kw: [{'role': 'user', 'content': 'accepted input'}])

    def run(task):
        task['content'] = 'terminal Turn, result not acknowledged'
        stamp_chat_task_terminal(task, status='done', finish_reason='stop')
        append_event(task, {'type': 'done', 'finishReason': 'stop'})
        task['_terminalPersistencePending'] = True

    monkeypatch.setattr('lib.tasks_pkg.orchestrator.api.run_task', run)
    with pytest.raises(WorkerDomainSettlementUnknown, match='persistence'):
        execute_claimed_attempt(context, context.claim.payload)
    with pytest.raises(WorkerDomainSettlementUnknown, match='persistence'):
        execute_claimed_attempt(context, context.claim.payload)


@pytest.mark.parametrize('internal', [False, True])
def test_stop_without_local_task_is_durable_and_cancel_handler_never_starts_work(claimed, monkeypatch, internal):
    from lib import turn_lifecycle as turns
    from lib.conversation_sync.worker_execution import cancel_claimed_attempt
    from lib.durable_worker import StorageWorkerJobStore
    context, pair, factory, owner = claimed
    monkeypatch.setattr('lib.conversation_sync.task_start.start_conversation_attempt_executor',
                        lambda *a, **kw: pytest.fail('cancelled attempt entered execution'))
    reason = 'dispatch_authority_lost' if internal else ''
    detail = 'Coordinator lease was revoked' if internal else ''
    receipt = turns.abort_attempt(pair['attempt']['attemptId'], user_id=owner, withdraw_dispatch=False,
                                  abort_reason=reason, abort_detail=detail)
    assert receipt['status'] == 'abort_signaled'
    assert turns.get_attempt(pair['attempt']['attemptId'], user_id=owner)['status'] == 'pending'
    context.observe_claim_state(StorageWorkerJobStore(client_factory=factory).claim_state(context.claim, now_ms=1))
    from lib.conversation_sync.worker_control import decode_cancellation
    assert decode_cancellation(context.cancellation_reason) == (reason, detail)
    # A later Stop must not replace the first durable cause, even without a carrier.
    turns.abort_attempt(pair['attempt']['attemptId'], user_id=owner, withdraw_dispatch=False,
                        abort_reason='replacement', abort_detail='must not win')
    context.observe_claim_state(StorageWorkerJobStore(client_factory=factory).claim_state(context.claim, now_ms=1))
    assert decode_cancellation(context.cancellation_reason) == (reason, detail)
    outcome = cancel_claimed_attempt(context, context.claim.payload)
    assert outcome.status == 'cancelled'
    settled = turns.get_attempt(pair['attempt']['attemptId'], user_id=owner)
    assert settled['status'] == ('failed' if internal else 'interrupted')
    assert settled['startedAt'] is None
    assert cancel_claimed_attempt(context, context.claim.payload) == outcome
