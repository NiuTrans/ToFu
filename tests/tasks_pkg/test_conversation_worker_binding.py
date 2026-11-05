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
