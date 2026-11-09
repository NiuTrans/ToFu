"""Durable execution authority travels with the exact task, never its config."""
import threading

import pytest

from lib.durable_worker import WorkerExecutionContext, WorkerJobClaim, WorkerLeaseLost
from lib.identity import PrincipalContext

pytestmark = pytest.mark.unit


def context():
    principal = PrincipalContext.user(subject_id='user:7', owner_user_id=7)
    return WorkerExecutionContext(WorkerJobClaim(
        task_id='conversation-attempt:attempt', task_kind='conversation-attempt',
        worker_id='worker', fencing_token=3, lease_deadline_ms=10_000,
        payload={'contract': 'tofu.conversation-attempt-job/v1',
                 'attemptId': 'attempt', 'turnId': 'turn', 'conversationId': 'conv',
                 'principal': principal.to_payload(), 'operation': 'create',
                 'baseProjectionRevision': 0}, principal=principal,
        replay_cursor=0, cancel_sequence=0, cancel_reason=''))


def task():
    return {'id': 'conversation-attempt:attempt', 'convId': 'conv',
            '_attemptId': 'attempt', '_turnId': 'turn', '_userId': 7,
            '_principalContext': context().principal.to_payload(),
            'abort_event': threading.Event(), 'config': {}}


@pytest.mark.parametrize('verdict', ['live', 'revoked', 'cancelled', 'stop', 'permanent'])
def test_detached_model_claim_waits_for_storage_without_losing_control(monkeypatch, verdict):
    from lib.durable_worker import StorageWorkerJobStore
    from lib.llm.llm_errors import AbortedError, ModelAdmissionRefused
    from lib.storage.errors import StorageError
    from lib.tasks_pkg.worker_binding import admit_model_dispatch
    owned, carrier = context(), task()
    carrier.update(id='detached-child', _conversationWorkerContext=owned)
    entries, probes = [], []

    def settled_entry(*args, **kwargs):
        entries.append(kwargs)
        return None  # The root Turn settled; its held job still owns this child.

    def claim_state(_store, claim, **kwargs):
        probes.append(claim)
        assert claim is owned.claim
        if verdict == 'permanent':
            raise StorageError('database_conflict', 'claim proof refused')
        if len(probes) == 1:
            if verdict == 'stop':
                carrier['abort_event'].set()
            raise StorageError('database_unavailable', 'claim proof temporarily unreachable')
        return {'ok': verdict != 'revoked', 'replayCursor': 0,
                'cancelSequence': int(verdict == 'cancelled'), 'cancelReason': 'Stop'}

    monkeypatch.setattr('lib.turn_lifecycle.mark_task_started', settled_entry)
    monkeypatch.setattr(StorageWorkerJobStore, 'claim_state', claim_state)
    if verdict == 'live':
        admit_model_dispatch(carrier, owned)
    elif verdict in {'stop', 'cancelled'}:
        with pytest.raises(AbortedError) as stopped:
            admit_model_dispatch(carrier, owned)
        assert not isinstance(stopped.value, ModelAdmissionRefused)
    else:
        with pytest.raises(ModelAdmissionRefused):
            admit_model_dispatch(carrier, owned)
    assert len(entries) == 1  # Retry only the read, never entry or a provider request.
    assert len(probes) == (1 if verdict in {'stop', 'permanent'} else 2)
    assert owned.authority_lost == (verdict == 'revoked')
    assert not carrier.get('_storageWaits')


def test_only_explicit_matching_context_can_supply_publication_authority():
    from lib.tasks_pkg.worker_binding import bind_conversation_worker, publication_arguments
    carrier = task()
    carrier['config']['_conversationWorkerContext'] = context()
    assert publication_arguments(carrier) == {}
    owned = context()
    bind_conversation_worker(carrier, owned)
    assert publication_arguments(carrier) == {
        'worker_claim': {'worker_id': 'worker', 'fencing_token': 3}}
    owned.mark_authority_lost()
    with pytest.raises(WorkerLeaseLost):
        publication_arguments(carrier, terminal=True)


@pytest.mark.parametrize(('field', 'value'), [
    ('id', 'other'), ('convId', 'other'), ('_turnId', 'other'),
    ('_attemptId', 'other'), ('_userId', 8),
])
def test_foreign_task_is_rejected_before_binding(field, value):
    from lib.tasks_pkg.worker_binding import bind_conversation_worker
    carrier = task()
    carrier[field] = value
    with pytest.raises((ValueError, PermissionError)):
        bind_conversation_worker(carrier, context())
    assert '_conversationWorkerContext' not in carrier


def test_cancellation_and_lost_authority_keep_distinct_verdicts():
    from lib.tasks_pkg.worker_binding import bind_conversation_worker, publication_arguments
    from lib.tasks_pkg.invocation import invocation_abort_requested
    cancelled, lost = task(), task()
    cancel_context, lost_context = context(), context()
    bind_conversation_worker(cancelled, cancel_context)
    bind_conversation_worker(lost, lost_context)
    cancel_context.observe_claim_state({'ok': True, 'cancelSequence': 1, 'cancelReason': 'Stop'})
    lost_context.mark_authority_lost()
    assert invocation_abort_requested(cancelled)
    assert invocation_abort_requested(lost)
    assert '_invocationDeliveryError' not in cancelled
    assert isinstance(lost['_invocationDeliveryError'], WorkerLeaseLost)
    assert publication_arguments(cancelled, terminal=True)['worker_claim']['fencing_token'] == 3
    assert publication_arguments(cancelled)['worker_claim']['fencing_token'] == 3
    from lib.durable_worker import WorkerCancellationRequested
    with pytest.raises(WorkerCancellationRequested):
        cancel_context.checkpoint()


def test_binding_cannot_replace_existing_invocation_control():
    from lib.tasks_pkg.worker_binding import bind_conversation_worker
    carrier = task()
    carrier['_invocationAbortCheck'] = lambda: False
    with pytest.raises(ValueError, match='control'):
        bind_conversation_worker(carrier, context())
    assert '_conversationWorkerContext' not in carrier


def test_child_inherits_claim_without_replacing_its_cancellation_scope():
    from lib.tasks_pkg.worker_binding import (
        bind_conversation_worker, inherit_conversation_worker, publication_arguments)
    parent, owned = task(), context()
    bind_conversation_worker(parent, owned)
    scope = lambda: False
    child = {**task(), 'id': 'child', '_parentTaskId': parent['id'],
             '_invocationAbortCheck': scope}
    inherit_conversation_worker(parent, child)
    assert child['_invocationAbortCheck'] is scope
    assert publication_arguments(child) == publication_arguments(parent)
    grandchild = {**task(), 'id': 'grandchild', '_parentTaskId': 'child'}
    inherit_conversation_worker(child, grandchild)
    owned.mark_authority_lost()
    for carrier in (child, grandchild):
        with pytest.raises(WorkerLeaseLost):
            publication_arguments(carrier, terminal=True)


def test_child_binding_refuses_wrong_lineage_and_cancelled_admission():
    from lib.durable_worker import WorkerCancellationRequested
    from lib.tasks_pkg.worker_binding import bind_conversation_worker, inherit_conversation_worker
    parent, owned = task(), context()
    bind_conversation_worker(parent, owned)
    child = {**task(), 'id': 'child', '_parentTaskId': 'other'}
    with pytest.raises(ValueError):
        inherit_conversation_worker(parent, child)
    child['_parentTaskId'] = parent['id']
    owned.observe_claim_state({'ok': True, 'cancelSequence': 1, 'cancelReason': 'Stop'})
    with pytest.raises(WorkerCancellationRequested):
        inherit_conversation_worker(parent, child)
    assert '_conversationWorkerContext' not in child


def test_revocation_during_registration_does_not_leak_a_task(monkeypatch):
    from lib.tasks_pkg.manager import create_task
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    owned = context()
    original = chat_task_runtime.create

    def create_then_revoke(**kwargs):
        carrier = original(**kwargs)
        owned.mark_authority_lost()
        return carrier

    monkeypatch.setattr(chat_task_runtime, 'create', create_then_revoke)
    with pytest.raises(WorkerLeaseLost):
        create_task('conv', [], {'_attemptId': 'attempt', '_turnId': 'turn'},
                    user_id=7, principal=owned.principal, worker_context=owned)
    assert chat_task_runtime.get(owned.claim.task_id) is None


def test_replay_fallback_preserves_claim_and_explicit_owner(monkeypatch):
    from types import SimpleNamespace
    from lib.tasks_pkg import event_log
    from lib.tasks_pkg.worker_binding import bind_conversation_worker, replay_publication_arguments
    carrier = task()
    bind_conversation_worker(carrier, context())
    captured = []

    def batch(**kw):
        captured.append(kw)
        raise RuntimeError('batch connection failed')

    def single(operation, payload, _command, **kw):
        assert operation == 'event.append'
        captured.append(payload)
        return {'inserted': False, 'rejected': 'worker_claim_stale'}

    monkeypatch.setattr(event_log, '_ensure_sidecar_batcher', lambda: SimpleNamespace(append=batch))
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kw: SimpleNamespace(command=single))
    with pytest.raises(event_log.EventAttemptStaleError):
        event_log.append_persistent_event(carrier['id'], 1, {'type': 'delta', 'content': 'x'},
                                          **replay_publication_arguments(carrier))
    assert len(captured) == 2
    assert all(row['worker_claim'] == {'worker_id': 'worker', 'fencing_token': 3}
               and row['user_id'] == 7 for row in captured)


@pytest.mark.parametrize('echo', [None, 'different', 'a' * 32])
def test_child_tool_entry_requires_exact_effect_acknowledgement(monkeypatch, echo):
    from types import SimpleNamespace
    from lib.tasks_pkg.child_invocation import create_child_invocation
    from lib.tasks_pkg.executor._effects import ToolEffectDurabilityError
    from lib.tasks_pkg.invocation_effects import persist_child_effect
    from lib.tasks_pkg.manager import discard_task
    from lib.tasks_pkg.worker_binding import bind_conversation_worker
    parent, owned = task(), context()
    bind_conversation_worker(parent, owned)
    births, writes = [], []

    def command(operation, payload, *args):
        if operation == 'invocation.register_child':
            assert payload['worker_claim'] == {'worker_id': 'worker', 'fencing_token': 3}
            identity = {key: value for key, value in payload.items() if key != 'worker_claim'}
            identity['archive_scope'] = f'invocation:7:{identity["child_task_id"]}'
            births.append(identity)
            return identity
        assert operation == 'task_results.checkpoint'
        writes.append(payload)
        return {'owned': True, 'version': 2, 'guard_contract': payload['guard_contract'],
                'tool_effect_intent': echo}

    def query(operation, payload):
        if operation == 'conversation.get':
            return {'id': 'conv'}
        return {'version': 1, 'value': {'task_id': births[-1]['child_task_id'],
            'user_id': 7, 'conv_id': 'conv', 'child_invocation': births[-1]}}

    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda **kw: SimpleNamespace(command=command, query=query))
    child = create_child_invocation(parent, model='offline-test', tools=[], objective='child',
        event_sink=lambda event: None, abort_check=lambda: False)
    row = {'toolName': 'controlled', 'toolCallId': 'call', 'roundNum': 1,
           'executionEffect': {'operationId': 'a' * 32, 'scope': 'invocation',
                               'state': 'entered', 'recovery': 'reconcile'}}
    try:
        if echo == 'a' * 32:
            persist_child_effect(child, row)
        else:
            with pytest.raises(ToolEffectDurabilityError, match='acknowledged'):
                persist_child_effect(child, row)
        assert writes[0]['tool_effect_intent'] == 'a' * 32
        assert writes[0]['worker_claim'] == {'worker_id': 'worker', 'fencing_token': 3}
    finally:
        discard_task(child['id'])


def test_project_grant_workspace_id_never_renames_the_claimed_task(monkeypatch):
    """Project grants capture a workspace task id; only the claim names the task."""
    from lib.tasks_pkg.manager import create_task
    from lib.tasks_pkg.manager._registry import _create_chat_task
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    owned = context()
    registered = []

    class Registered(Exception):
        pass

    def stop_at_registration(**kwargs):
        registered.append(kwargs['task_id'])
        raise Registered

    monkeypatch.setattr(chat_task_runtime, 'create', stop_at_registration)
    config = {'_attemptId': 'attempt', '_turnId': 'turn',
              '_projectDispatchWorkspaceTaskId': 'project-worker:assignment'}
    with pytest.raises(Registered):
        create_task('conv', [], dict(config), user_id=7, principal=owned.principal,
                    worker_context=owned)
    assert registered == [owned.claim.task_id]
    # An explicitly prepared identity is a caller's claim, so a mismatch still refuses.
    with pytest.raises(ValueError, match='prepared task identity'):
        _create_chat_task('conv', [], dict(config), user_id=7, principal=owned.principal,
                          prepared_task_id='project-worker:assignment', worker_context=owned)
    assert registered == [owned.claim.task_id]
