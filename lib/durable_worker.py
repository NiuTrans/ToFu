"""Synchronous durable-worker runner with injected execution and storage ports.

The module owns lease mechanics only.  It registers no production handlers:
each task kind stays unclaimable until its registration declares durable event
replay, terminal accounting/admission settlement, cooperative cancellation,
and fencing for externally visible side effects.  Those declarations still
require crash-injection E2E before lifecycle composition may enable workers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
import hashlib
import threading
import time
from typing import Any, Literal, Protocol

from lib.identity import PrincipalContext, require_user_id
from lib.log import get_logger


logger = get_logger(__name__)


WorkerTerminalStatus = Literal['succeeded', 'failed', 'cancelled']
WorkerRunState = Literal[
    'idle',
    'succeeded',
    'failed',
    'cancelled',
    'lease_lost',
    'protocol_error',
    'terminal_unconfirmed',
]


class WorkerConfigurationError(RuntimeError):
    """No task kind has proved the prerequisites required for claiming."""


class WorkerProtocolError(RuntimeError):
    """The durable job document violates the worker execution contract."""


class WorkerLeaseLost(RuntimeError):
    """The execution no longer proves the database fencing token."""


class WorkerCancellationRequested(RuntimeError):
    """The owner issued a durable cancellation command."""


@dataclass(frozen=True, slots=True)
class WorkerKindSafety:
    """Explicit readiness evidence required before a kind may be claimed."""

    durable_event_replay: bool = False
    terminal_accounting_settlement: bool = False
    cooperative_cancellation: bool = False
    externally_visible_side_effect_fencing: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            'durable_event_replay',
            'terminal_accounting_settlement',
            'cooperative_cancellation',
            'externally_visible_side_effect_fencing',
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(
                    f'worker safety {field_name} must be boolean')

    @property
    def production_ready(self) -> bool:
        return not self.blockers

    @property
    def blockers(self) -> tuple[str, ...]:
        missing = []
        if not self.durable_event_replay:
            missing.append('durable_event_replay')
        if not self.terminal_accounting_settlement:
            missing.append('terminal_accounting_settlement')
        if not self.cooperative_cancellation:
            missing.append('cooperative_cancellation')
        if not self.externally_visible_side_effect_fencing:
            missing.append('externally_visible_side_effect_fencing')
        return tuple(missing)


@dataclass(frozen=True, slots=True)
class WorkerJobOutcome:
    """A handler result after its domain settlement is already durable."""

    status: WorkerTerminalStatus
    result_ref: str = ''
    error: Mapping[str, Any] = field(default_factory=dict)
    replay_cursor: int = 0

    def __post_init__(self) -> None:
        if self.status not in {'succeeded', 'failed', 'cancelled'}:
            raise ValueError('invalid worker terminal status')
        if len(self.result_ref) > 1024:
            raise ValueError('worker result_ref exceeds 1024 characters')
        if not isinstance(self.error, Mapping):
            raise TypeError('worker outcome error must be a mapping')
        if (not isinstance(self.replay_cursor, int)
                or isinstance(self.replay_cursor, bool)
                or self.replay_cursor < 0):
            raise ValueError('worker replay_cursor must be non-negative')


@dataclass(frozen=True, slots=True)
class WorkerJobClaim:
    """Validated authority returned by ``worker_job.claim_next``."""

    task_id: str
    task_kind: str
    worker_id: str
    fencing_token: int
    lease_deadline_ms: int
    payload: Mapping[str, Any]
    principal: PrincipalContext
    replay_cursor: int
    cancel_sequence: int
    cancel_reason: str

    @classmethod
    def from_document(
        cls, document: Mapping[str, Any], *, expected_worker_id: str,
    ) -> 'WorkerJobClaim':
        task_id = str(document.get('taskId') or '')
        task_kind = str(document.get('taskKind') or '')
        claim_owner = str(document.get('claimOwner') or '')
        status = str(document.get('status') or '')
        payload = document.get('payload')
        if not task_id or len(task_id) > 256:
            raise WorkerProtocolError('claimed worker job has invalid taskId')
        if not task_kind or len(task_kind) > 128:
            raise WorkerProtocolError('claimed worker job has invalid taskKind')
        if claim_owner != expected_worker_id or status != 'running':
            raise WorkerProtocolError(
                'claimed worker job does not belong to this live worker')
        if not isinstance(payload, Mapping):
            raise WorkerProtocolError('claimed worker job payload is invalid')
        contract = str(payload.get('contract') or '')
        raw_principal = payload.get('principal')
        if not contract or not isinstance(raw_principal, Mapping):
            raise WorkerProtocolError(
                'claimed worker job lacks contract or principal')
        try:
            principal = PrincipalContext.from_payload(raw_principal)
            owner_user_id = principal.require_owner(context='durable worker job')
            document_owner = require_user_id(
                document.get('userId'), context='durable worker job')
        except (PermissionError, TypeError, ValueError) as exc:
            raise WorkerProtocolError(
                'claimed worker job principal is invalid') from exc
        if dict(raw_principal) != principal.to_payload():
            raise WorkerProtocolError(
                'claimed worker job principal is not canonical')
        if owner_user_id != document_owner:
            raise WorkerProtocolError(
                'claimed worker job principal does not match job owner')
        if (principal.tenant_id or '') != str(document.get('tenantId') or ''):
            raise WorkerProtocolError(
                'claimed worker job principal does not match job tenant')
        try:
            fencing_token = int(document.get('fencingToken'))
            lease_deadline_ms = int(document.get('leaseDeadlineMs'))
            replay_cursor = int(document.get('replayCursor') or 0)
            cancel_sequence = int(document.get('cancelSequence') or 0)
        except (TypeError, ValueError) as exc:
            raise WorkerProtocolError(
                'claimed worker job lease fields are invalid') from exc
        if fencing_token <= 0 or lease_deadline_ms <= 0:
            raise WorkerProtocolError('claimed worker job has no live fence')
        if replay_cursor < 0 or cancel_sequence < 0:
            raise WorkerProtocolError(
                'claimed worker job cursor fields are invalid')
        return cls(
            task_id=task_id,
            task_kind=task_kind,
            worker_id=expected_worker_id,
            fencing_token=fencing_token,
            lease_deadline_ms=lease_deadline_ms,
            payload=dict(payload),
            principal=principal,
            replay_cursor=replay_cursor,
            cancel_sequence=cancel_sequence,
            cancel_reason=str(document.get('cancelReason') or ''),
        )


@dataclass(slots=True)
class WorkerExecutionContext:
    """Cooperative fence/cancellation context passed to one kind handler."""

    claim: WorkerJobClaim
    _authority_lost: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False)
    _cancel_requested: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False)
    _state_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False)
    _replay_cursor: int = field(default=0, init=False, repr=False)
    _cancel_reason: str = field(default='', init=False, repr=False)

    def __post_init__(self) -> None:
        self._replay_cursor = self.claim.replay_cursor
        self._cancel_reason = self.claim.cancel_reason
        if self.claim.cancel_sequence > 0:
            self._cancel_requested.set()

    @property
    def principal(self) -> PrincipalContext:
        return self.claim.principal

    @property
    def fencing_token(self) -> int:
        return self.claim.fencing_token

    @property
    def cancellation_reason(self) -> str:
        with self._state_lock:
            return self._cancel_reason

    @property
    def replay_cursor(self) -> int:
        with self._state_lock:
            return self._replay_cursor

    @property
    def authority_lost(self) -> bool:
        return self._authority_lost.is_set()

    @property
    def cancellation_requested(self) -> bool:
        return self._cancel_requested.is_set()

    def advance_replay_cursor(self, replay_cursor: int) -> None:
        if (not isinstance(replay_cursor, int)
                or isinstance(replay_cursor, bool)
                or replay_cursor < 0):
            raise ValueError('replay cursor must be non-negative')
        with self._state_lock:
            self._replay_cursor = max(self._replay_cursor, replay_cursor)

    def observe_claim_state(self, state: Mapping[str, Any]) -> None:
        if state.get('ok') is not True:
            self._authority_lost.set()
            return
        self.advance_replay_cursor(int(state.get('replayCursor') or 0))
        if int(state.get('cancelSequence') or 0) > 0:
            with self._state_lock:
                self._cancel_reason = str(state.get('cancelReason') or '')
            self._cancel_requested.set()

    def observe_heartbeat(self, response: Mapping[str, Any]) -> None:
        if response.get('ok') is not True:
            self._authority_lost.set()
            return
        job = response.get('job')
        if not isinstance(job, Mapping):
            self._authority_lost.set()
            return
        self.observe_claim_state({
            'ok': True,
            'replayCursor': job.get('replayCursor', 0),
            'cancelSequence': job.get('cancelSequence', 0),
            'cancelReason': job.get('cancelReason', ''),
        })

    def mark_authority_lost(self) -> None:
        self._authority_lost.set()

    def checkpoint(self) -> None:
        """Fail before another event, terminal write, or fenced side effect."""
        if self.authority_lost:
            raise WorkerLeaseLost(
                f'worker fence lost for {self.claim.task_id}')
        if self.cancellation_requested:
            raise WorkerCancellationRequested(
                self.cancellation_reason or 'worker job was cancelled')


WorkerJobHandler = Callable[
    [WorkerExecutionContext, Mapping[str, Any]], WorkerJobOutcome
]


@dataclass(frozen=True, slots=True)
class WorkerKindRegistration:
    """One injected handler and the safety properties it has proved."""

    task_kind: str
    handler: WorkerJobHandler
    safety: WorkerKindSafety = field(default_factory=WorkerKindSafety)

    def __post_init__(self) -> None:
        if not self.task_kind or len(self.task_kind) > 128:
            raise ValueError('worker task kind must be 1..128 characters')
        if not callable(self.handler):
            raise TypeError('worker kind handler must be callable')
        if not isinstance(self.safety, WorkerKindSafety):
            raise TypeError('worker kind safety must be WorkerKindSafety')


class WorkerJobStore(Protocol):
    """Synchronous database-authority port used by the runner."""

    def claim_next(
        self, *, worker_id: str, task_kinds: tuple[str, ...],
        now_ms: int, lease_ms: int,
    ) -> Mapping[str, Any] | None: ...

    def claim_state(
        self, claim: WorkerJobClaim, *, now_ms: int,
    ) -> Mapping[str, Any]: ...

    def heartbeat(
        self, claim: WorkerJobClaim, *, now_ms: int, lease_ms: int,
        replay_cursor: int,
    ) -> Mapping[str, Any]: ...

    def complete(
        self, claim: WorkerJobClaim, *, now_ms: int,
        outcome: WorkerJobOutcome,
    ) -> Mapping[str, Any]: ...


def _storage_command_id(label: str, *parts: object) -> str:
    digest = hashlib.sha256(
        '\x1f'.join(str(part) for part in parts).encode('utf-8')
    ).hexdigest()[:32]
    return f'durable-worker:{label}:{digest}'


class StorageWorkerJobStore:
    """Production storage adapter; PostgreSQL/SQLite semantics stay in Sidecar."""

    @staticmethod
    def _client(*, write: bool = False) -> Any:
        from lib.storage import get_storage_client

        return get_storage_client(write=write)

    def claim_next(
        self, *, worker_id: str, task_kinds: tuple[str, ...],
        now_ms: int, lease_ms: int,
    ) -> Mapping[str, Any] | None:
        return self._client(write=True).command(
            'worker_job.claim_next', {
                'worker_id': worker_id,
                'task_kinds': list(task_kinds),
                'now_ms': now_ms,
                'lease_ms': lease_ms,
            },
            _storage_command_id('claim', worker_id, now_ms),
        )

    def claim_state(
        self, claim: WorkerJobClaim, *, now_ms: int,
    ) -> Mapping[str, Any]:
        return self._client().query('worker_job.claim_state', {
            'task_id': claim.task_id,
            'worker_id': claim.worker_id,
            'fencing_token': claim.fencing_token,
            'now_ms': now_ms,
        })

    def heartbeat(
        self, claim: WorkerJobClaim, *, now_ms: int, lease_ms: int,
        replay_cursor: int,
    ) -> Mapping[str, Any]:
        return self._client(write=True).command(
            'worker_job.heartbeat', {
                'task_id': claim.task_id,
                'worker_id': claim.worker_id,
                'fencing_token': claim.fencing_token,
                'now_ms': now_ms,
                'lease_ms': lease_ms,
                'replay_cursor': replay_cursor,
            },
            _storage_command_id(
                'heartbeat', claim.task_id, claim.fencing_token, now_ms),
        )

    def complete(
        self, claim: WorkerJobClaim, *, now_ms: int,
        outcome: WorkerJobOutcome,
    ) -> Mapping[str, Any]:
        return self._client(write=True).command(
            'worker_job.complete', {
                'task_id': claim.task_id,
                'worker_id': claim.worker_id,
                'fencing_token': claim.fencing_token,
                'now_ms': now_ms,
                'terminal_status': outcome.status,
                'result_ref': outcome.result_ref,
                'error': dict(outcome.error),
                'replay_cursor': outcome.replay_cursor,
            },
            _storage_command_id(
                'complete', claim.task_id, claim.fencing_token,
                outcome.status, now_ms),
        )


@dataclass(frozen=True, slots=True)
class WorkerRunResult:
    state: WorkerRunState
    task_id: str = ''
    task_kind: str = ''
    fencing_token: int = 0
    detail: str = ''


class SynchronousWorkerRunner:
    """Claim one job, supervise its lease, and CAS its terminal job state."""

    def __init__(
        self,
        *,
        worker_id: str,
        store: WorkerJobStore,
        registrations: Iterable[WorkerKindRegistration],
        lease_ms: int = 60_000,
        heartbeat_interval_ms: int = 20_000,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        normalized_worker_id = str(worker_id or '').strip()
        if not normalized_worker_id or len(normalized_worker_id) > 256:
            raise ValueError('worker_id must be 1..256 characters')
        if not 10_000 <= lease_ms <= 300_000:
            raise ValueError('worker lease_ms must be 10000..300000')
        if not 1 <= heartbeat_interval_ms < lease_ms:
            raise ValueError('heartbeat interval must be positive and below lease')
        by_kind: dict[str, WorkerKindRegistration] = {}
        for registration in registrations:
            if registration.task_kind in by_kind:
                raise ValueError(
                    f'duplicate worker task kind: {registration.task_kind}')
            by_kind[registration.task_kind] = registration
        self.worker_id = normalized_worker_id
        self.store = store
        self.lease_ms = lease_ms
        self.heartbeat_interval_ms = heartbeat_interval_ms
        self._registrations = by_kind
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))

    @property
    def claimable_task_kinds(self) -> tuple[str, ...]:
        return tuple(sorted(
            kind for kind, registration in self._registrations.items()
            if registration.safety.production_ready
        ))

    def require_claimable_task_kinds(self) -> tuple[str, ...]:
        claimable = self.claimable_task_kinds
        if claimable:
            return claimable
        blockers = {
            kind: registration.safety.blockers
            for kind, registration in sorted(self._registrations.items())
        }
        raise WorkerConfigurationError(
            'no production-ready worker task kinds; blockers='
            f'{blockers!r}')

    @staticmethod
    def _run_result(
        state: WorkerRunState,
        claim: WorkerJobClaim | None = None,
        *,
        detail: str = '',
    ) -> WorkerRunResult:
        if claim is None:
            return WorkerRunResult(state=state, detail=detail)
        return WorkerRunResult(
            state=state,
            task_id=claim.task_id,
            task_kind=claim.task_kind,
            fencing_token=claim.fencing_token,
            detail=detail,
        )

    @staticmethod
    def _failed_outcome(exc: Exception, replay_cursor: int) -> WorkerJobOutcome:
        detail = str(exc).strip()[:4000] or type(exc).__name__
        return WorkerJobOutcome(
            status='failed',
            error={
                'kind': 'worker_execution_failed',
                'message': 'Durable worker handler failed.',
                'detail': detail,
                'exceptionType': type(exc).__name__,
            },
            replay_cursor=replay_cursor,
        )

    def _start_heartbeat_monitor(
        self, context: WorkerExecutionContext,
    ) -> tuple[threading.Event, threading.Thread]:
        stop = threading.Event()

        def monitor() -> None:
            interval_s = self.heartbeat_interval_ms / 1000.0
            while not stop.wait(interval_s):
                try:
                    response = self.store.heartbeat(
                        context.claim,
                        now_ms=self._now_ms(),
                        lease_ms=self.lease_ms,
                        replay_cursor=context.replay_cursor,
                    )
                    context.observe_heartbeat(response)
                except Exception:
                    # A worker that cannot prove lease renewal must stop
                    # producing events/side effects and leave takeover to the
                    # database deadline.  It must not guess that a failed RPC
                    # means the old fence is still authoritative.
                    context.mark_authority_lost()
                if context.authority_lost:
                    return

        thread = threading.Thread(
            target=monitor,
            name=f'durable-worker-heartbeat:{context.claim.task_id[:32]}',
            daemon=True,
        )
        thread.start()
        return stop, thread

    def run_once(self) -> WorkerRunResult:
        task_kinds = self.require_claimable_task_kinds()
        document = self.store.claim_next(
            worker_id=self.worker_id,
            task_kinds=task_kinds,
            now_ms=self._now_ms(),
            lease_ms=self.lease_ms,
        )
        if document is None:
            return self._run_result('idle')
        try:
            claim = WorkerJobClaim.from_document(
                document, expected_worker_id=self.worker_id)
        except WorkerProtocolError as exc:
            return self._run_result('protocol_error', detail=str(exc))
        registration = self._registrations.get(claim.task_kind)
        if (registration is None
                or not registration.safety.production_ready
                or claim.task_kind not in task_kinds):
            # Defense in depth if a storage adapter ever violates its explicit
            # kind filter.  No unknown payload reaches a handler.
            return self._run_result(
                'protocol_error', claim,
                detail='storage returned an unsupported worker task kind',
            )

        context = WorkerExecutionContext(claim)
        try:
            state = self.store.claim_state(claim, now_ms=self._now_ms())
            context.observe_claim_state(state)
        except Exception:
            context.mark_authority_lost()
        if context.authority_lost:
            return self._run_result(
                'lease_lost', claim, detail='initial fence proof failed')

        stop, monitor = self._start_heartbeat_monitor(context)
        outcome: WorkerJobOutcome
        try:
            if context.cancellation_requested:
                outcome = WorkerJobOutcome(
                    status='cancelled',
                    error={
                        'kind': 'cancelled',
                        'message': context.cancellation_reason
                        or 'Worker job was cancelled before execution.',
                    },
                    replay_cursor=context.replay_cursor,
                )
            else:
                outcome = registration.handler(context, claim.payload)
                if not isinstance(outcome, WorkerJobOutcome):
                    raise WorkerProtocolError(
                        'worker handler returned an invalid outcome')
                context.advance_replay_cursor(outcome.replay_cursor)
                context.checkpoint()
        except WorkerCancellationRequested as exc:
            outcome = WorkerJobOutcome(
                status='cancelled',
                error={'kind': 'cancelled', 'message': str(exc)},
                replay_cursor=context.replay_cursor,
            )
        except WorkerLeaseLost:
            context.mark_authority_lost()
            outcome = WorkerJobOutcome(
                status='failed', replay_cursor=context.replay_cursor)
        except Exception as exc:
            logger.exception(
                '[DurableWorker] job execution failed task=%s', claim.task_id)
            outcome = self._failed_outcome(exc, context.replay_cursor)
        finally:
            stop.set()
            monitor.join(timeout=max(1.0, self.heartbeat_interval_ms / 1000.0))

        if context.authority_lost:
            return self._run_result(
                'lease_lost', claim,
                detail='fence was lost while the handler was running',
            )
        if context.cancellation_requested and outcome.status != 'cancelled':
            outcome = WorkerJobOutcome(
                status='cancelled',
                error={
                    'kind': 'cancelled',
                    'message': context.cancellation_reason
                    or 'Worker job was cancelled.',
                },
                replay_cursor=context.replay_cursor,
            )

        try:
            terminal = self.store.complete(
                claim, now_ms=self._now_ms(), outcome=outcome)
        except Exception as exc:
            logger.exception(
                '[DurableWorker] terminal storage result is unknown job=%s',
                claim.task_id,
            )
            return self._run_result(
                'terminal_unconfirmed', claim,
                detail=f'terminal storage result is unknown: {type(exc).__name__}',
            )
        if terminal.get('ok') is not True:
            return self._run_result(
                'lease_lost', claim,
                detail=str(terminal.get('error') or 'terminal fence refused'),
            )
        return self._run_result(outcome.status, claim)


__all__ = [
    'StorageWorkerJobStore',
    'SynchronousWorkerRunner',
    'WorkerCancellationRequested',
    'WorkerConfigurationError',
    'WorkerExecutionContext',
    'WorkerJobClaim',
    'WorkerJobOutcome',
    'WorkerJobStore',
    'WorkerKindRegistration',
    'WorkerKindSafety',
    'WorkerLeaseLost',
    'WorkerProtocolError',
    'WorkerRunResult',
]
