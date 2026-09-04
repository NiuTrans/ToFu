"""Atomic dispatch and fail-closed durable-worker runner contracts."""

from __future__ import annotations

from collections.abc import Mapping
import threading
import time
import uuid

import pytest

from lib.durable_worker import (
    SynchronousWorkerRunner,
    WorkerConfigurationError,
    WorkerExecutionContext,
    WorkerJobClaim,
    WorkerJobOutcome,
    WorkerKindRegistration,
    WorkerKindSafety,
)
from lib.identity import PrincipalContext
from lib.storage.errors import StorageError


pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit


_READY_SAFETY = WorkerKindSafety(
    durable_event_replay=True,
    terminal_accounting_settlement=True,
    cooperative_cancellation=True,
    externally_visible_side_effect_fencing=True,
)


def _pending_attempt(chat_sidecar, *, user_id: int = 71):
    del chat_sidecar
    from lib.turn_lifecycle import create_turn_pair

    nonce = uuid.uuid4().hex
    created = create_turn_pair(
        f'durable-worker-conv:{nonce}',
        command_id=f'durable-worker-command:{nonce}',
        input_projection={'content': 'execute durably'},
        config={'model': 'test-model'},
        user_id=user_id,
        conversation_defaults={
            'allowCreate': True,
            'title': 'Durable worker test',
            'settings': {},
        },
    )
    return created


def test_conversation_attempt_dispatch_atomically_binds_one_durable_job(
    chat_sidecar,
):
    from lib.storage import get_storage_client
    from lib.turn_lifecycle import dispatch_attempt_to_worker, get_attempt

    created = _pending_attempt(chat_sidecar)
    attempt_id = created['attempt']['attemptId']
    principal = PrincipalContext.user(
        subject_id='test-api-key',
        owner_user_id=71,
        tenant_id='tenant-test',
        scopes={'conversation.execute'},
    )
    client = get_storage_client()
    before_dispatch_head = client.query('turn.sync.snapshot', {
        'conversation_id': created['turn']['conversationId'],
        'user_id': 71,
    })['syncSequence']

    dispatched = dispatch_attempt_to_worker(
        principal, attempt_id, now_ms=10_000)
    task_id = f'conversation-attempt:{attempt_id}'
    assert dispatched['created'] is True
    assert dispatched['attempt']['taskId'] == task_id
    # Dispatch BINDS (task_id + queued job) but leaves the attempt pending;
    # the physical pending→running transition is a separate worker-entry
    # operation (mark_task_started → turn.attempt.start).
    assert dispatched['attempt']['status'] == 'pending'
    assert dispatched['job']['taskId'] == task_id
    assert dispatched['job']['status'] == 'queued'
    assert dispatched['job']['payload'] == {
        'contract': 'tofu.conversation-attempt-job/v1',
        'conversationId': created['turn']['conversationId'],
        'turnId': created['turn']['turnId'],
        'attemptId': attempt_id,
        'principal': principal.to_payload(),
        'baseProjectionRevision': created['attempt']['baseProjectionRevision'],
        'operation': 'generate',
    }
    dispatch_changes = client.query('turn.sync.changes', {
        'conversation_id': created['turn']['conversationId'],
        'user_id': 71,
        'after': before_dispatch_head,
    })['events']
    assert len(dispatch_changes) == 1
    assert dispatch_changes[0]['type'] == 'attempt.event'
    assert dispatch_changes[0]['payload']['event']['payload']['status'] == (
        'pending')

    durable_attempt = get_attempt(attempt_id, user_id=71)
    durable_job = client.query('worker_job.get', {
        'task_id': task_id,
        'user_id': 71,
    })
    assert durable_attempt['taskId'] == durable_job['taskId'] == task_id

    replay = dispatch_attempt_to_worker(
        principal, attempt_id, now_ms=20_000)
    assert replay['created'] is False
    assert replay['idempotentReplay'] is True
    assert replay['job']['taskId'] == task_id


def test_atomic_dispatch_rejects_principal_mismatch_without_creating_job(
    chat_sidecar,
):
    from lib.storage import get_storage_client
    from lib.turn_lifecycle import get_attempt

    created = _pending_attempt(chat_sidecar)
    attempt_id = created['attempt']['attemptId']
    task_id = f'conversation-attempt:{attempt_id}'
    other_owner = PrincipalContext.user(
        subject_id='wrong-owner', owner_user_id=72)

    with pytest.raises(StorageError) as raised:
        get_storage_client(write=True).command(
            'turn.attempt.dispatch_worker', {
                'attempt_id': attempt_id,
                'user_id': 71,
                'principal': other_owner.to_payload(),
                'now_ms': 10_000,
            },
            f'wrong-owner:{uuid.uuid4().hex}',
        )
    assert raised.value.code == 'database_conflict'
    assert get_attempt(attempt_id, user_id=71)['taskId'] == ''
    assert get_storage_client().query('worker_job.get', {
        'task_id': task_id, 'user_id': 71,
    }) is None


def test_atomic_dispatch_payload_limit_rolls_back_attempt_binding(chat_sidecar):
    from lib.storage import get_storage_client
    from lib.turn_lifecycle import dispatch_attempt_to_worker, get_attempt

    created = _pending_attempt(chat_sidecar)
    attempt_id = created['attempt']['attemptId']
    task_id = f'conversation-attempt:{attempt_id}'
    oversized_scopes = {
        f'scope:{index:05d}:' + ('x' * 110)
        for index in range(9_000)
    }
    principal = PrincipalContext.user(
        subject_id='oversized-principal',
        owner_user_id=71,
        scopes=oversized_scopes,
    )

    with pytest.raises(StorageError) as raised:
        dispatch_attempt_to_worker(principal, attempt_id, now_ms=10_000)
    assert raised.value.code == 'storage_payload_too_large'
    assert get_attempt(attempt_id, user_id=71)['taskId'] == ''
    assert get_storage_client().query('worker_job.get', {
        'task_id': task_id, 'user_id': 71,
    }) is None


def _job_document(
    *, task_kind: str = 'conversation-attempt', cancel_sequence: int = 0,
) -> dict:
    principal = PrincipalContext.user(
        subject_id='runner-owner', owner_user_id=81, tenant_id='tenant-runner')
    return {
        'taskId': 'runner-job',
        'userId': 81,
        'tenantId': 'tenant-runner',
        'taskKind': task_kind,
        'payload': {
            'contract': 'tofu.conversation-attempt-job/v1',
            'conversationId': 'runner-conv',
            'turnId': 'runner-turn',
            'attemptId': 'runner-attempt',
            'principal': principal.to_payload(),
        },
        'status': 'running',
        'claimOwner': 'replica-a/worker-1',
        'fencingToken': 7,
        'leaseDeadlineMs': 61_000,
        'replayCursor': 3,
        'cancelSequence': cancel_sequence,
        'cancelReason': 'owner stopped it' if cancel_sequence else '',
    }


class _FakeStore:
    def __init__(
        self,
        document: Mapping | None,
        *,
        initial_state: Mapping | None = None,
        heartbeat_response: Mapping | None = None,
        terminal_response: Mapping | None = None,
    ) -> None:
        self.document = document
        self.initial_state = initial_state or {
            'ok': True,
            'cancelSequence': 0,
            'cancelReason': '',
            'replayCursor': 3,
        }
        self.heartbeat_response = heartbeat_response or {
            'ok': True,
            'job': {
                'cancelSequence': 0,
                'cancelReason': '',
                'replayCursor': 3,
            },
        }
        self.terminal_response = terminal_response or {'ok': True, 'job': {}}
        self.claim_calls: list[dict] = []
        self.heartbeat_calls: list[dict] = []
        self.complete_calls: list[WorkerJobOutcome] = []
        self.heartbeat_called = threading.Event()

    def claim_next(self, **kwargs):
        self.claim_calls.append(dict(kwargs))
        return self.document

    def claim_state(self, claim: WorkerJobClaim, *, now_ms: int):
        del claim, now_ms
        return self.initial_state

    def heartbeat(
        self, claim: WorkerJobClaim, *, now_ms: int, lease_ms: int,
        replay_cursor: int,
    ):
        self.heartbeat_calls.append({
            'claim': claim,
            'now_ms': now_ms,
            'lease_ms': lease_ms,
            'replay_cursor': replay_cursor,
        })
        self.heartbeat_called.set()
        return self.heartbeat_response

    def complete(
        self, claim: WorkerJobClaim, *, now_ms: int,
        outcome: WorkerJobOutcome,
    ):
        del claim, now_ms
        self.complete_calls.append(outcome)
        return self.terminal_response


def _registration(
    handler,
    *,
    task_kind: str = 'conversation-attempt',
    safety: WorkerKindSafety = _READY_SAFETY,
):
    return WorkerKindRegistration(
        task_kind=task_kind, handler=handler, safety=safety)


def test_runner_claims_only_kinds_with_all_safety_evidence():
    store = _FakeStore(_job_document())

    def handler(
        context: WorkerExecutionContext, payload: Mapping,
    ) -> WorkerJobOutcome:
        assert payload['attemptId'] == 'runner-attempt'
        assert context.principal.require_owner() == 81
        assert context.fencing_token == 7
        context.advance_replay_cursor(9)
        return WorkerJobOutcome(
            status='succeeded',
            result_ref='turn-attempt:runner-attempt',
            replay_cursor=9,
        )

    runner = SynchronousWorkerRunner(
        worker_id='replica-a/worker-1',
        store=store,
        registrations=[
            _registration(handler),
            _registration(
                handler,
                task_kind='unsafe-kind',
                safety=WorkerKindSafety(durable_event_replay=True),
            ),
        ],
        now_ms=lambda: 1_000,
    )
    result = runner.run_once()

    assert result.state == 'succeeded'
    assert store.claim_calls[0]['task_kinds'] == ('conversation-attempt',)
    assert store.complete_calls[0].replay_cursor == 9


def test_runner_with_no_production_ready_kind_fails_before_claim():
    store = _FakeStore(_job_document())
    runner = SynchronousWorkerRunner(
        worker_id='replica-a/worker-1',
        store=store,
        registrations=[_registration(
            lambda _context, _payload: WorkerJobOutcome(status='succeeded'),
            safety=WorkerKindSafety(durable_event_replay=True),
        )],
    )

    with pytest.raises(
        WorkerConfigurationError,
        match='terminal_accounting_settlement',
    ):
        runner.run_once()
    assert store.claim_calls == []


def test_safety_evidence_requires_real_booleans():
    with pytest.raises(TypeError, match='must be boolean'):
        WorkerKindSafety(  # type: ignore[arg-type]
            durable_event_replay='yes',
            terminal_accounting_settlement=True,
            cooperative_cancellation=True,
            externally_visible_side_effect_fencing=True,
        )


def test_runner_never_executes_kind_outside_explicit_claim_filter():
    called = False

    def handler(_context, _payload):
        nonlocal called
        called = True
        return WorkerJobOutcome(status='succeeded')

    store = _FakeStore(_job_document(task_kind='unknown-kind'))
    runner = SynchronousWorkerRunner(
        worker_id='replica-a/worker-1',
        store=store,
        registrations=[_registration(handler)],
    )

    result = runner.run_once()
    assert result.state == 'protocol_error'
    assert called is False
    assert store.complete_calls == []


def test_runner_rejects_noncanonical_principal_before_handler():
    called = False

    def handler(_context, _payload):
        nonlocal called
        called = True
        return WorkerJobOutcome(status='succeeded')

    document = _job_document()
    document['payload']['principal']['scopes'] = 'conversation.execute'
    store = _FakeStore(document)
    runner = SynchronousWorkerRunner(
        worker_id='replica-a/worker-1',
        store=store,
        registrations=[_registration(handler)],
    )

    result = runner.run_once()
    assert result.state == 'protocol_error'
    assert called is False


def test_runner_observes_durable_cancel_before_handler_and_acks_cancelled():
    called = False

    def handler(_context, _payload):
        nonlocal called
        called = True
        return WorkerJobOutcome(status='succeeded')

    store = _FakeStore(
        _job_document(),
        initial_state={
            'ok': True,
            'cancelSequence': 1,
            'cancelReason': 'stop before execution',
            'replayCursor': 3,
        },
    )
    runner = SynchronousWorkerRunner(
        worker_id='replica-a/worker-1',
        store=store,
        registrations=[_registration(handler)],
    )

    result = runner.run_once()
    assert result.state == 'cancelled'
    assert called is False
    assert store.complete_calls[0].status == 'cancelled'


def test_runner_heartbeat_fence_loss_prevents_terminal_write():
    store = _FakeStore(
        _job_document(),
        heartbeat_response={'ok': False, 'error': 'stale_fence'},
    )

    def handler(
        context: WorkerExecutionContext, _payload: Mapping,
    ) -> WorkerJobOutcome:
        assert store.heartbeat_called.wait(0.5)
        deadline = time.monotonic() + 0.5
        while not context.authority_lost and time.monotonic() < deadline:
            threading.Event().wait(0.001)
        context.checkpoint()
        raise AssertionError('checkpoint must reject the old fence')

    runner = SynchronousWorkerRunner(
        worker_id='replica-a/worker-1',
        store=store,
        registrations=[_registration(handler)],
        lease_ms=10_000,
        heartbeat_interval_ms=5,
    )

    result = runner.run_once()
    assert result.state == 'lease_lost'
    assert store.heartbeat_calls
    assert store.complete_calls == []


def test_runner_refused_terminal_cas_reports_lease_loss():
    store = _FakeStore(
        _job_document(),
        terminal_response={
            'ok': False,
            'error': 'stale_fence_or_cancelled',
        },
    )
    runner = SynchronousWorkerRunner(
        worker_id='replica-a/worker-1',
        store=store,
        registrations=[_registration(
            lambda _context, _payload: WorkerJobOutcome(status='succeeded'))],
    )

    result = runner.run_once()
    assert result.state == 'lease_lost'
    assert result.detail == 'stale_fence_or_cancelled'
