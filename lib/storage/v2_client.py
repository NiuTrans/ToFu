"""Application-facing storage.v2 client for the native Tofu-DB backend.

This is the ``StorageClient``-compatible surface the repositories and services
already consume, backed directly by a ``tofu-db serve`` process over its
authenticated storage.v2 protocol (no Python sidecar in the hot path). The
wire codec is the single shared implementation in
``tofu_db/wire.py`` (also used by the migration importer and the
semantic differential gate); this module only adapts its session/response
model to the synchronous ``query``/``command``/``health``/``metrics``/
``maintenance`` interface and the stable :class:`StorageError` taxonomy the
application already handles.

Protocol source of truth: ``contracts/storage_v2.json`` and
``packages/tofu-db/src/daemon.rs`` / ``server.rs``. The operation catalog the
application issues is frozen in ``contracts/storage_operations_v1.json`` and is
served 1:1 by tofu-db's semantic executor.
"""

from __future__ import annotations

import random
import threading
import time
import uuid
from collections.abc import Mapping
from typing import Any

from lib.log import get_logger
from lib.storage.commit_events import publish_committed_events
from lib.storage.errors import StorageError
from lib.storage.event_composition import EventBatchCompositionBudget, native_event_batch_composition_budget
from lib.storage.frame_admission import (
    FrameByteAdmission,
    process_response_frame_admission,
)
from lib.storage.retry_log import retry_log_throttle
from lib.storage.v2_semantics import application_notice, application_result, request_payload
from lib.storage.protocol import validate_operation

logger = get_logger(__name__)

from scripts.generated_storage_operations import STORAGE_SCHEMA_VERSION
from tofu_db.generated_v2 import MAX_COMMIT_NOTICE_KEYS, COMMITTED_EVENTS_CONTRACT
from tofu_db.wire import (
    StorageV2AdmissionRejected,
    StorageV2ConnectionClosedError,
    StorageV2Error,
    StorageV2ProtocolError,
    StorageV2Session,
)


# Transient classified failures that are safe to replay on a fresh connection.
# Mirrors lib/storage/client.py: reads are replay-safe; a command stays
# fail-fast unless the peer explicitly proved it was rejected before dispatch.
# storage.v2 addition: the native engine emits database_busy only from
# classified pre-mutation refusals (WAL-rotation backpressure, capacity
# bounds), so a same command-id retry of a busy command cannot double-apply.
# Busy is flow control, not failure: a busy command gets a deeper bounded
# budget with exponential backoff inside the caller's deadline, while every
# other command failure keeps the shallow fail-fast budget. Backoff starts
# fine-grained: rotation install windows are millisecond-scale, so a 10 ms
# first retry turns an invisible wait into a p99 event.
_RETRYABLE_CODES = {'database_timeout', 'database_busy', 'database_unavailable'}
_BUSY_COMMAND_ATTEMPTS = 16
_BUSY_COMMAND_BASE_DELAY_S = 0.002
_BUSY_COMMAND_MAX_DELAY_S = 0.250

# Availability-class failures (starved/unreachable authority, pool admission
# exhaustion) escalate beyond the flat ``retry_after_ms`` hint: that hint is
# tuned for a healthy authority's millisecond blips. Against a starved
# authority every retry re-pays the full execution window, and a fleet of
# caller threads re-attempting at 50–100 ms spacing is the storm that keeps
# the authority starved (2026-09-22 cgroup memory-pressure incident: the
# supervisor withheld recycling while 120 sync threads retry-spun the
# pool). The FIRST retry keeps the server hint — a healthy blip pays nothing
# — and consecutive availability failures space out exponentially with
# jitter, still inside the caller's aggregate budget. The escalation engages
# only when the remaining budget affords it (4x the wait), so a small-budget
# caller keeps its full attempt count at the blip hint. Busy COMMANDS keep
# their own finer WAL-rotation schedule below.
_AVAILABILITY_BACKOFF_CODES = frozenset(
    {'database_timeout', 'database_busy', 'database_unavailable'})
_AVAILABILITY_BACKOFF_BASE_S = 0.5
_AVAILABILITY_BACKOFF_CAP_S = 8.0

# Aggregate retry-budget floor for commands. The aggregate cap is
# ``timeout * attempts``; with the 20 s production deadline and 3 attempts
# a command already rides a 60 s window, well past a ~10 s supervised
# recycle. A caller that overrides with a short explicit deadline must
# still ride the same bounded window — replaying on the receipt proof
# above is safe at any budget, and failing fast only converts a slow
# authority into a user-visible 503. The floor is capped so a wedged
# authority still fails fast relative to the request lifecycle.
_COMMAND_MIN_RETRY_BUDGET_S = 45.0
# tofu-db's semantic executor emits storage.v1 error codes directly, except
# for these v2-specific classifications, which map onto the stable taxonomy.
_V2_TO_STORAGE_CODE = {
    'authority_scope_mismatch': 'database_forbidden',
    'deadline_elapsed': 'database_timeout',
    'database_resource_exhausted': 'database_unavailable',
}

# Deterministic request-oversize rejections from tofu_db/wire.py, raised
# BEFORE any byte is written to the socket (the per-operation payload bound
# and the streamed-chunk bound). They are caller-correctable boundary
# failures, not codec misalignment: the session stays aligned and the
# rejection classifies exactly like the v1 sidecar payload cap
# (``storage_payload_too_large``), so every downstream deterministic-overflow
# fence (lib/turn_lifecycle.py::_is_frame_overflow_error, slim settle, the
# cooperative abort) engages uniformly across backends.
_REQUEST_OVERSIZE_MARKERS = (
    'payload exceeds its operation bound',
    'request requires too many chunks',
)


def _record_rpc(kind: str, operation: str, started_at: float,
                outcome: str) -> None:
    """Best-effort latency record; metrics must never break the RPC path.

    Mirrors lib/storage/client.py so the native backend feeds the same
    ``tofu_storage_rpc_seconds`` histogram the v1 sidecar client feeds.
    """
    try:
        from lib.observability import record_storage_rpc
        record_storage_rpc(
            kind, operation, time.monotonic() - started_at, outcome)
    except Exception as _e:
        logger.debug('record rpc failed: %s', _e)
        pass


# Pool sizing: the daemon advertises `maximumConnections` in its readiness
# resourceBudget (launch-probe derived: CPU × 16, bounded by memory headroom
# and the wire-contract ceiling); the pool takes at most half — other client
# processes (the supervisor's health probes, a second app role) must always
# find a free connection. There is no client-side constant cap: the
# machine-probed daemon budget is the only ceiling, per the scaling charter
# (bounds prevent runaway growth, they never starve a capable machine).
# Without a budget the lean-daemon profile is the safe fallback: four
# connections. 2026-09-22: removed the hardcoded cap (8 → 16 → none) — on a
# slow authority volume (FUSE block reads at ~7 ms) a 16-session pool
# exhausted under one slow-scan burst, failing user-facing RPCs with
# database_busy while the daemon still advertised free connections.
_POOL_SIZE_LEAN_FALLBACK = 4
# Socket-level slack beyond the per-attempt wire deadline: the daemon answers
# `deadline_elapsed` at the wire deadline, so the socket only needs to outlive
# the network round trip that carries that answer.
_SOCKET_TIMEOUT_SLACK_S = 1.0

# Idle-session freshness bound. The daemon's mid-frame socket I/O timeout is
# 30 s (packages/tofu-db/src/listener.rs `io_timeout`), and daemon builds may
# retire a session that idles past their policy — older builds close it at
# that 30 s mark. The pool therefore retires an idle session well before any
# daemon build would: a reconnect costs ~130 µs of loopback TCP+Hello,
# amortized over the whole freshness window. The acquire-time liveness probe
# (``StorageV2Session.probe_liveness``) covers the residual race — a daemon
# restart, crash, or idle close that landed between release and reuse.
_SESSION_IDLE_CAP_S = 20.0

class _ConnectionClosedError(StorageError):
    """``database_unavailable`` for a peer close at a frame boundary.

    No response byte was emitted, so the replay-safety proof matches
    ``deadline_elapsed``: a command that committed before its response was
    lost left a durable (tenant, owner, command_id) receipt, and a
    same-command-id replay returns the recorded outcome instead of
    double-applying. Only this subclass unlocks command replay on
    ``database_unavailable``; every other source stays fail-fast for
    commands.
    """

    __slots__ = ()


def _pool_size_from_budget(budget: Mapping[str, Any] | None) -> int:
    maximum = (budget or {}).get('maximumConnections')
    if type(maximum) is not int or maximum < 1:
        return _POOL_SIZE_LEAN_FALLBACK
    return max(1, maximum // 2)


class _SessionPool:
    """Demand-created exclusive sessions with a bounded idle reuse cache.

    Size controls retained idle sockets, never active requests. Each caller
    immediately claims an idle session or a new creation reservation, then
    probes/negotiates outside the lock under its own attempt deadline. Close
    and deadline fences are checked before returning the owned session.
    Closing sockets remain observable until teardown finishes; they cannot
    block unrelated callers. Broken sessions are never reused.
    """

    def __init__(self, factory, size: int):
        if size < 1:
            raise ValueError('storage.v2 idle session target must be positive')
        self._factory = factory
        self._size = size
        self._idle: list = []
        self._live = 0
        self._retiring = 0
        self._closed = False
        self._completed = 0
        self._condition = threading.Condition()

    @property
    def size(self) -> int:
        return self._size

    def resize(self, size: int) -> None:
        """Resize only idle retention; socket teardown runs outside the lock."""
        if size < 1:
            return
        retired = []
        with self._condition:
            self._size = size
            while len(self._idle) > size:
                session, _released_at = self._idle.pop()
                self._retiring += 1
                retired.append(session)
        for session in retired:
            self._retire_detached_session(session)

    def _retire_detached_session(self, session) -> None:
        try:
            self._close_quietly(session)
        finally:
            with self._condition:
                self._live -= 1
                self._retiring -= 1

    def _discard_reserved_session(self, session) -> None:
        with self._condition:
            self._retiring += 1
        self._retire_detached_session(session)

    def resource_snapshot(self) -> dict[str, int]:
        with self._condition:
            return {'capacity': self._size, 'active_capacity': 0,
                    'idle_reuse_capacity': self._size,
                    'used': self._live - len(self._idle), 'idle': len(self._idle),
                    'retiring': self._retiring, 'waiting': 0,
                    'completed': self._completed}

    @staticmethod
    def _close_quietly(session) -> None:
        closer = getattr(session, 'close', None)
        if closer is None:
            return
        try:
            closer()
        except Exception as _e:
            logger.debug('close quietly failed: %s', _e)
            pass

    @staticmethod
    def _reusable(session, released_at: float) -> bool:
        """One idle session may be reused only while it is fresh and the
        peer has not already closed it (a FIN/RST queued on an idle socket
        is the daemon's idle reaping or a restart — both are invisible until
        probed). Sessions without a probe (test doubles) rely on the
        freshness cap alone."""
        if time.monotonic() - released_at > _SESSION_IDLE_CAP_S:
            return False
        probe = getattr(session, 'probe_liveness', None)
        if probe is None:
            return True
        try:
            return bool(probe())
        except Exception as _e:
            logger.debug('check reusable failed: %s', _e)
            return False

    def acquire(self, timeout: float):
        """Claim immediately, then validate the exclusive socket unlocked."""
        deadline = time.monotonic() + max(0.001, timeout)
        while True:
            session = None
            released_at = None
            with self._condition:
                if self._closed:
                    raise StorageError('database_unavailable',
                        'storage.v2 client is closed', True, 100,
                        request_not_dispatched=True)
                if self._idle:
                    session, released_at = self._idle.pop()
                else:
                    self._live += 1
            try:
                if released_at is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise StorageError('database_timeout',
                            'storage.v2 connection budget elapsed', True, 25,
                            request_not_dispatched=True)
                    session = self._factory(remaining)
                    reusable = True
                else:
                    reusable = self._reusable(session, released_at)
                with self._condition:
                    if self._closed:
                        raise StorageError('database_unavailable',
                            'storage.v2 client is closed', True, 100,
                            request_not_dispatched=True)
                    if time.monotonic() >= deadline:
                        raise StorageError('database_timeout',
                            'storage.v2 connection budget elapsed', True, 25,
                            request_not_dispatched=True)
            except BaseException:
                self._discard_reserved_session(session)
                raise
            if reusable:
                return session
            self._discard_reserved_session(session)

    def release(self, session, *, broken: bool) -> None:
        with self._condition:
            self._completed += 1
            retire = broken or self._closed or len(self._idle) >= self._size
            if retire:
                self._retiring += 1
            else:
                self._idle.append((session, time.monotonic()))
        if retire:
            self._retire_detached_session(session)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            idle, self._idle = self._idle, []
            self._retiring += len(idle)
        for session, _released_at in idle:
            self._retire_detached_session(session)


class StorageV2Client:
    """Synchronous ``StorageClient``-compatible client over storage.v2.

    RPCs borrow demand-created persistent authenticated sessions
    (``_SessionPool``): a fresh connection per RPC paid TCP+Hello+negotiation
    (~130 µs) against ~5 µs of in-daemon point-read execution. Idle reuse
    derives from the daemon's advertised connection budget
    (``set_readiness_budget``), a session that hits a transport/protocol
    error is retired, and replay-safety classification is unchanged from the
    per-call-session model (reads replay-safe; commands fail-fast without
    pre-dispatch proof). The owner/tenant scope is fixed at Hello time by
    the daemon, so it is supplied once here and reused for every session.
    """

    def __init__(
        self,
        host: str,
        port: int,
        secret: bytes,
        *,
        owner_id: int,
        tenant_id: int | None,
        timeout: float = 5.0,
        read_attempts: int = 3,
        session_pool_size: int | None = None,
        response_frame_admission: FrameByteAdmission | None = None,
    ):
        if host not in {'127.0.0.1', 'localhost', '::1'}:
            raise ValueError('storage.v2 is loopback-only')
        if not 32 <= len(secret) <= 256:
            raise ValueError('storage.v2 secret must contain 32..256 bytes')
        if owner_id <= 0 or (tenant_id is not None and tenant_id <= 0):
            raise ValueError('storage.v2 owner/tenant IDs must be positive')
        self._host = host
        self._port = int(port)
        self._secret = bytes(secret)
        self._owner_id = int(owner_id)
        self._tenant_id = tenant_id if tenant_id is None else int(tenant_id)
        self._timeout = max(0.05, float(timeout))
        self._read_attempts = max(1, int(read_attempts))
        self._readiness_lock = threading.Lock()
        self._readiness_budget: dict[str, Any] = {}
        self._event_composition_budget: EventBatchCompositionBudget | None = None
        # The process-wide in-flight response budget, shared with the
        # storage.v1 client. Without it, a full pool of streamed responses
        # could retain 8 × 64 MiB with no process-level bound (2026-09-21
        # hardening review); per-response wire caps stay as the second tier.
        self._response_frame_admission = (
            response_frame_admission
            if response_frame_admission is not None
            else process_response_frame_admission()
        )
        size = (session_pool_size if session_pool_size is not None
                else _POOL_SIZE_LEAN_FALLBACK)
        self._pool = _SessionPool(self._open_session, size)
        # Health owns a separate lazy session so query socket failures and
        # slow application work cannot prevent observer-based supervision.
        self._health_lock = threading.Lock()
        self._health_session: StorageV2Session | None = None

    @property
    def endpoint(self) -> tuple[str, int]:
        return (self._host, self._port)

    def _new_session(self, timeout: float) -> StorageV2Session:
        """One disposable session (supervisor probes, tests).

        The pooled RPC path uses ``_open_session``; this per-call shape
        remains for callers that explicitly want connection isolation.
        """
        return self._open_session(timeout)

    def _open_session(self, timeout: float | None = None) -> StorageV2Session:
        timeout = self._timeout if timeout is None else timeout
        session = StorageV2Session(
            self._host,
            self._port,
            self._secret,
            owner_id=self._owner_id,
            tenant_id=self._tenant_id,
            schema_ids=(STORAGE_SCHEMA_VERSION,),
            timeout=timeout,
            response_admission=self._response_frame_admission,
        )
        # Pooled sessions rely on the caller to re-arm the per-attempt
        # budget via set_timeout before each request; sessions owned by a
        # single caller (the health lane) skip that step, so honor the
        # requested timeout up front. Callers that re-arm anyway get the
        # same value twice — a no-op.
        set_timeout = getattr(session, 'set_timeout', None)
        if set_timeout is not None:
            set_timeout(timeout)
        return session

    def close(self) -> None:
        """Close every idle pooled session and the health lane; in-flight
        sessions retire on release. Idempotent; a closed client answers
        classified errors."""
        self._pool.close()
        with self._health_lock:
            session, self._health_session = self._health_session, None
        if session is not None:
            _SessionPool._close_quietly(session)

    def __del__(self) -> None:  # best-effort GC teardown; close() is explicit
        # Pool-only, matching the pre-health-lane GC contract: closing the
        # health session here would fire during GC at an arbitrary point
        # (e.g. inside another test's monkeypatched session fake). The
        # daemon reaps a dropped socket as an ordinary client EOF, so an
        # un-closed health lane is never a leak; explicit close() is the
        # deterministic teardown and closes both.
        try:
            self._pool.close()
        except Exception as _e:
            logger.debug('finalize client failed: %s', _e)
            pass

    def resource_snapshot(self) -> dict[str, int]:
        snapshot = self._pool.resource_snapshot()
        with self._readiness_lock:
            snapshot['ceiling'] = _pool_size_from_budget(self._readiness_budget)
        return snapshot

    def resource_frame_admission(self):
        return self._response_frame_admission

    def resize_resource_capacity(self, capacity: int) -> None:
        with self._readiness_lock:
            ceiling = _pool_size_from_budget(self._readiness_budget)
        if type(capacity) is not int or not 1 <= capacity <= ceiling:
            raise ValueError('storage capacity exceeds selected authority budget')
        self._pool.resize(capacity)

    def set_readiness_budget(self, budget: Mapping[str, Any] | None) -> None:
        """Record the daemon's credential-free readiness resourceBudget.

        The supervisor captures this from the storage.ready envelope so
        ``health()``/``metrics()`` can report the probe-derived budgets without
        a storage RPC. Only aggregate numbers/booleans are accepted.
        """
        recorded = dict(budget or {})
        try:
            composition = native_event_batch_composition_budget(recorded.get('eventBatchComposition'))
        except Exception:
            composition = None
        with self._readiness_lock:
            self._readiness_budget = recorded
            self._event_composition_budget = composition
        self._pool.resize(_pool_size_from_budget(budget))

    def event_batch_composition_budget(self) -> EventBatchCompositionBudget | None:
        """Read only the native format declaration cached at daemon readiness.

        Older daemons and standalone clients without a readiness declaration
        retain legacy batching; this accessor never probes the server.
        """
        with self._readiness_lock:
            return self._event_composition_budget

    def _call(
        self,
        kind: str,
        operation: str,
        payload: Mapping[str, Any] | None,
        *,
        deadline: float | None,
        command_id: str | None = None,
    ) -> Any:
        operation = validate_operation(operation)
        timeout = self._timeout if deadline is None else max(0.001, float(deadline))
        read_replay_safe = kind in {'query', 'health', 'metrics'}
        attempts = self._read_attempts if read_replay_safe or kind == 'command' else 1
        aggregate_budget = timeout * attempts
        if kind == 'command':
            aggregate_budget = max(aggregate_budget,
                                   _COMMAND_MIN_RETRY_BUDGET_S)
        # A finite FUSE rotation can exceed sixteen fast retries while the
        # caller still has most of its deadline. Busy means no mutation; ride
        # the existing time budget with capped backoff and the same receipt ID.
        # The count is a secondary bound derived from that budget, not another
        # hidden one-second deadline. Other failures keep their shallow cap.
        busy_attempts = max(_BUSY_COMMAND_ATTEMPTS,
            int(aggregate_budget / _BUSY_COMMAND_MAX_DELAY_S) + 16)
        loop_attempts = busy_attempts if kind == 'command' else attempts
        stop_at = time.monotonic() + aggregate_budget
        # tofu-db rejects any state-mutating op that lacks a command_id
        # (mutates_state() == kind != Query), and maintenance-kind ops such as
        # project_brain.recovery.snapshot mutate. Only reads may omit it.
        if kind != 'query' and command_id is None:
            command_id = f'{operation}:{uuid.uuid4().hex}'
        wire_command_id = None if kind == 'query' else command_id
        started_at = time.monotonic()
        last_error = None
        for attempt in range(loop_attempts):
            remaining = stop_at - time.monotonic()
            if remaining <= 0 and last_error is not None:
                _record_rpc(kind, operation, started_at, 'error')
                raise last_error
            # Each replay gets a fresh execution window within the original
            # aggregate retry budget. Reusing the first wire deadline makes
            # every retry after a slow attempt arrive already expired.
            attempt_timeout = min(timeout, max(0.001, stop_at - time.monotonic()))
            deadline_unix_ms = int((time.time() + attempt_timeout) * 1000)
            try:
                result = self._attempt(
                    operation, payload,
                    timeout=attempt_timeout,
                    command_id=wire_command_id,
                    deadline_unix_ms=deadline_unix_ms,
                )
                _record_rpc(kind, operation, started_at, 'success')
                return result
            except StorageError as exc:
                last_error = exc
                # ``deadline_elapsed`` arrives with retryable=False because
                # that single wire flag also gates command replay, but both
                # server variants are replay-safe here: admission rejects
                # pre-dispatch, and an execution deadline aborts the
                # transaction pre-commit — committed commands always leave a
                # durable (tenant, owner, command_id) receipt, so a
                # same-command-id replay returns the recorded outcome
                # instead of double-applying (the classification
                # lib/evolution/records.py already applies).
                timeout_transient = exc.code == 'database_timeout'
                retry_classified = (
                    (exc.retryable or timeout_transient)
                    and exc.code in _RETRYABLE_CODES)
                busy_command = kind == 'command' and exc.code == 'database_busy'
                # A peer close at a frame boundary shares the
                # deadline_elapsed replay proof above (no response byte was
                # emitted; a committed command left a durable receipt), so a
                # same-command-id replay returns the recorded outcome instead
                # of double-applying. Only ``_ConnectionClosedError`` widens
                # command replay this way; every other database_unavailable
                # source stays fail-fast for commands.
                closed_transient = isinstance(exc, _ConnectionClosedError)
                # A supervised-restart window (or any transport refusal
                # before the first response byte) is replay-safe for
                # commands by the same proof as ``_ConnectionClosedError``:
                # no response byte means either the command never reached
                # the authority, or it committed and left a durable
                # (tenant, owner, command_id) receipt whose replay returns
                # the recorded outcome instead of double-applying. Without
                # this the ~10 s recycle 503'd every in-flight command
                # within milliseconds (2026-09-24 desktop/token and
                # task-row upsert outage) even though the caller's RPC
                # deadline exceeded the restart window.
                unavailable_transient = exc.code == 'database_unavailable'
                command_replay_safe = kind == 'command' and (
                    exc.request_not_dispatched or busy_command
                    or timeout_transient or closed_transient
                    or unavailable_transient)
                replay_safe = read_replay_safe or command_replay_safe
                attempt_cap = (
                    busy_attempts if busy_command else attempts)
                exhausted = attempt + 1 >= attempt_cap
                if not retry_classified or not replay_safe or exhausted:
                    _record_rpc(kind, operation, started_at, 'error')
                    raise
                remaining = stop_at - time.monotonic()
                if remaining <= 0.05:
                    _record_rpc(kind, operation, started_at, 'error')
                    raise
                # Local admission pressure needs congestion backoff; the
                # millisecond schedule is only for authority WAL flow control.
                if busy_command and not exc.request_not_dispatched:
                    delay = min(
                        _BUSY_COMMAND_BASE_DELAY_S * (2 ** min(attempt, 16)),
                        _BUSY_COMMAND_MAX_DELAY_S,
                        remaining,
                    )
                else:
                    hint = (exc.retry_after_ms or 50) / 1000.0
                    escalated = min(
                        _AVAILABILITY_BACKOFF_BASE_S * (2 ** (attempt - 1)),
                        _AVAILABILITY_BACKOFF_CAP_S,
                    )
                    # Escalate only when the aggregate budget affords
                    # patience: a small-budget caller keeps the blip hint
                    # and its full attempt count instead of sleeping the
                    # whole budget away in one wait.
                    if (exc.code in _AVAILABILITY_BACKOFF_CODES
                            and attempt >= 1
                            and escalated * 4 <= remaining):
                        delay = escalated
                    else:
                        delay = min(hint, remaining)
                if retry_log_throttle().allow(f'v2:{kind}:{operation}'):
                    logger.warning(
                        'storage.v2 retrying %s %s (attempt %d/%d) in %.2fs '
                        'after %s: %s',
                        kind, operation, attempt + 2, attempt_cap, delay,
                        exc.code, str(exc)[:160])
                time.sleep(min(remaining, delay + random.uniform(0.0, min(delay * 0.25, 1.0))))
        _record_rpc(kind, operation, started_at, 'error')
        raise StorageError(  # unreachable: loop either returns or raises
            'database_internal', 'Storage retry loop exited unexpectedly')

    def _attempt(
        self,
        operation: str,
        payload: Mapping[str, Any] | None,
        *,
        timeout: float,
        command_id: str | None,
        deadline_unix_ms: int,
    ) -> Any:
        body = request_payload(operation, payload, owner_id=self._owner_id)
        stop_at = time.monotonic() + timeout
        session = None
        broken = False
        try:
            session = self._pool.acquire(timeout)
            remaining = stop_at - time.monotonic()
            if remaining <= 0:
                raise StorageError('database_timeout',
                    'storage.v2 request expired before dispatch', True, 25,
                    request_not_dispatched=True)
            # Per-attempt caller budget on the reused socket: the daemon
            # answers at the wire deadline; the socket bound only covers a
            # wedged peer (see _SOCKET_TIMEOUT_SLACK_S).
            set_timeout = getattr(session, 'set_timeout', None)
            if set_timeout is not None:
                set_timeout(remaining + _SOCKET_TIMEOUT_SLACK_S)
            response = session.request(
                operation,
                body,
                command_id=command_id,
                deadline_unix_ms=deadline_unix_ms,
            )
            if response.status != 0:
                raise self._to_storage_error(response, operation)
            return response.json()
        except StorageError:
            raise
        except StorageV2Error as exc:
            # Defensive: request() raises this only for transport-class
            # failures today, whose session state is unknowable — retire it.
            broken = True
            raise self._to_storage_error(exc, operation) from exc
        except StorageV2ConnectionClosedError as exc:
            # The peer closed at a frame boundary without emitting one
            # response byte (idle reaping, daemon restart): an availability
            # failure, not a framing violation. Classified retryable so the
            # replay policy in _call can reissue on a fresh session.
            broken = True
            raise _ConnectionClosedError(
                'database_unavailable',
                'storage.v2 authority closed the connection',
                True,
                50,
            ) from exc
        except StorageV2ProtocolError as exc:
            broken = True
            raise StorageError(
                'database_protocol_error',
                f'storage.v2 protocol violation: {exc}',
            ) from exc
        except StorageV2AdmissionRejected as exc:
            # The process response-byte budget declined this response within
            # its bounded wait — the same classified retryable rejection the
            # storage.v1 frame admission raises, never a silent wait. The
            # frame stream is desynchronized, so the session is retired.
            broken = True
            raise StorageError(
                'database_unavailable',
                'storage.v2 response frame budget exhausted',
                True,
                50,
            ) from exc
        except (TimeoutError, OSError) as exc:
            broken = True
            raise StorageError(
                'database_unavailable',
                'storage.v2 authority is unavailable',
                True,
                100,
            ) from exc
        except (ValueError, TypeError) as exc:
            if type(exc) is ValueError and any(
                    marker in str(exc) for marker in _REQUEST_OVERSIZE_MARKERS):
                # Raised before dispatch: no request byte reached the wire,
                # so the pooled session is provably still aligned and must
                # NOT be retired — sustained oversize rejections (an
                # executor emitting multi-MiB frames) would otherwise churn
                # the pool into a reconnect storm.
                raise StorageError(
                    'storage_payload_too_large',
                    f'storage.v2 request rejected before dispatch: {exc}',
                ) from exc
            # Native codec/contract failures must never escape as raw
            # builtins: wire.py raises ValueError on oversize payloads,
            # TypeError on unencodable field values, and the response
            # decoders raise JSONDecodeError/UnicodeDecodeError (both
            # ValueError) — tofu_db/embedded.py's _loads mirrors them for
            # the in-process transport. Map to the v1 error class
            # (lib/storage/protocol.py) and conservatively retire the
            # session: local codec state is no longer provably aligned.
            broken = True
            raise StorageError(
                'database_protocol_error',
                f'storage.v2 codec failure: {type(exc).__name__}: {exc}',
            ) from exc
        finally:
            if session is not None:
                self._pool.release(session, broken=broken)

    @staticmethod
    def _to_storage_error(exc: StorageV2Error | Any,
                          operation: str = '') -> StorageError:
        wire_code = getattr(exc, 'error_code', None) or getattr(exc, 'code', '') or ''
        code = _V2_TO_STORAGE_CODE.get(wire_code, wire_code)
        retryable = bool(getattr(exc, 'retryable', False))
        message = (
            getattr(exc, 'error_message', None)
            or getattr(exc, 'message', '')
            or 'storage.v2 request failed'
        )
        if retryable:
            # ``database_resource_exhausted`` is the authority shedding load
            # ("back off now"), not a millisecond blip. Honoring the 50 ms
            # blip hint against it re-hammers an already-exhausted authority —
            # the 2026-09-24 turn.sync.changes storm retried at a flat 50 ms
            # for hours. Give load-shed the availability base as its FIRST
            # retry hint so even a 3-attempt query backs off meaningfully;
            # the jitter in _call then decorrelates the fleet. A generic
            # blip keeps the cheap 50 ms hint.
            if wire_code == 'database_resource_exhausted':
                retry_after_ms = int(_AVAILABILITY_BACKOFF_BASE_S * 1000)
            else:
                retry_after_ms = 50
        else:
            retry_after_ms = None
        return StorageError(
            code,
            message,
            retryable,
            retry_after_ms,
            operation_id=operation,
        )

    def query(
        self,
        operation: str,
        payload: Mapping[str, Any] | None = None,
        deadline: float | None = None,
    ) -> Any:
        return application_result(
            operation, self._call('query', operation, payload, deadline=deadline)
        )

    def command(
        self,
        operation: str,
        payload: Mapping[str, Any] | None,
        command_id: str | None,
        priority: str = 'user',
        deadline: float | None = None,
    ) -> Any:
        result = self._call(
            'command', operation, payload, deadline=deadline,
            command_id=command_id,
        )
        if not isinstance(result, Mapping) or '_storageCommitContract' not in result:
            return application_result(operation, result)
        if result.get('_storageCommitContract') != COMMITTED_EVENTS_CONTRACT:
            raise StorageError(
                'database_protocol_error',
                'Storage committed-event envelope version mismatch',
            )
        events = result.get('events')
        if ('value' not in result or not isinstance(events, list)
                or len(events) > MAX_COMMIT_NOTICE_KEYS
                or not all(isinstance(event, Mapping) for event in events)):
            raise StorageError(
                'database_protocol_error',
                'Malformed storage committed-event envelope',
            )
        publish_committed_events([
            application_notice(event, owner_id=self._owner_id) for event in events
        ])
        return application_result(operation, result.get('value'))

    def health(self, deadline: float | None = 2.0) -> Any:
        """Daemon-reported health, or a synthetic liveness probe on old builds.

        Current tofu-db builds serve an authenticated ``system.health``
        control operation answered from observer handles — never the authority
        mutex — so it responds even while a commit holds the authority.
        Older pinned binaries reject it as ``unknown_operation``; the fallback
        keeps the previous ``conversation.count`` round-trip semantic.

        The probe runs on a dedicated session (``_health_call``), never the
        query pool, so slow or broken application sessions cannot fail it.
        """
        with self._readiness_lock:
            budget = dict(self._readiness_budget)
        result: dict[str, Any] = {
            'ready': False,
            'backend': 'tofudb',
            'protocol': 'storage.v2',
            'owner_id': self._owner_id,
            'tenant_id': self._tenant_id,
        }
        try:
            reported = self._health_call(
                'system.health', {}, deadline=deadline)
        except StorageError as exc:
            if exc.code != 'unknown_operation':
                # Preserve sanitized classification without exposing error text
                # or changing the false readiness verdict / probe retry budget.
                result['probe_error'] = exc.code
                if budget:
                    result['resourceBudget'] = budget
                return result
        else:
            if isinstance(reported, Mapping):
                result.update(reported)
                result.update({
                    'ready': bool(reported.get('ready')),
                    'backend': 'tofudb',
                    'protocol': 'storage.v2',
                    'owner_id': self._owner_id,
                    'tenant_id': self._tenant_id,
                })
                if budget and 'resourceBudget' not in result:
                    result['resourceBudget'] = budget
                return result
        # Fallback for binaries without the control operation: readiness is
        # the ability to complete an authenticated semantic round-trip.
        try:
            self._health_call(
                'conversation.count', {}, deadline=deadline)
            result['ready'] = True
        except StorageError as exc:
            result['ready'] = False
            result['probe_error'] = exc.code
        if budget:
            result['resourceBudget'] = budget
        return result

    def _health_call(
        self,
        operation: str,
        payload: Mapping[str, Any] | None,
        *,
        deadline: float | None,
    ) -> Any:
        """One bounded RPC on the dedicated health session, never the pool.

        A single attempt: the supervisor's liveness cadence is the retry
        loop, so this layer must not multiply the probe deadline by a retry
        budget (a 1.5 s probe would otherwise block up to 3×). The daemon's
        wire deadline is the caller's deadline exactly as in ``_call``; the
        socket bound only covers a wedged peer (``_SOCKET_TIMEOUT_SLACK_S``).
        A failed session is dropped and rebuilt on the next probe, so a
        wedged or recycled daemon never latches the lane closed. Serialized
        by ``_health_lock``; the lock is held for at most the bounded wire
        deadline, so concurrent probers queue rather than contending.
        """
        operation = validate_operation(operation)
        timeout = self._timeout if deadline is None else max(0.001, float(deadline))
        deadline_unix_ms = int((time.time() + timeout) * 1000)
        started_at = time.monotonic()
        if not self._health_lock.acquire(timeout=timeout):
            raise StorageError('database_timeout',
                'storage.v2 health lane deadline elapsed', True, 25,
                request_not_dispatched=True)
        try:
            remaining = timeout - (time.monotonic() - started_at)
            if remaining <= 0:
                raise StorageError('database_timeout',
                    'storage.v2 health lane deadline elapsed', True, 25,
                    request_not_dispatched=True)
            session = self._health_session
            broken = False
            try:
                if session is None:
                    session = self._open_session(remaining)
                    self._health_session = session
                remaining = timeout - (time.monotonic() - started_at)
                if remaining <= 0:
                    raise StorageError('database_timeout',
                        'storage.v2 health request expired before dispatch', True, 25,
                        request_not_dispatched=True)
                set_timeout = getattr(session, 'set_timeout', None)
                if set_timeout is not None:
                    set_timeout(remaining)
                body = request_payload(
                    operation, payload, owner_id=self._owner_id)
                response = session.request(
                    operation,
                    body,
                    command_id=None,
                    deadline_unix_ms=deadline_unix_ms,
                )
                if response.status != 0:
                    raise self._to_storage_error(response, operation)
                _record_rpc('health', operation, started_at, 'success')
                return response.json()
            except StorageError:
                raise
            except StorageV2Error as exc:
                broken = True
                raise self._to_storage_error(exc, operation) from exc
            except StorageV2ConnectionClosedError as exc:
                # An idle-reaped/restarted peer closes at a frame boundary.
                # Retire this dedicated lane just like the pooled path; the
                # next probe opens a new session without retrying this probe.
                broken = True
                raise StorageError('database_unavailable',
                    'storage.v2 authority closed the health connection', True, 100) from exc
            except StorageV2ProtocolError as exc:
                broken = True
                raise StorageError('database_protocol_error',
                    'storage.v2 health protocol violation') from exc
            except StorageV2AdmissionRejected as exc:
                broken = True
                raise StorageError('database_unavailable',
                    'storage.v2 response frame budget exhausted', True, 50) from exc
            except (TimeoutError, OSError) as exc:
                broken = True
                raise StorageError(
                    'database_unavailable',
                    'storage.v2 authority is unavailable',
                    True,
                    100,
                ) from exc
            except (ValueError, TypeError) as exc:
                broken = True
                raise StorageError(
                    'database_protocol_error',
                    f'storage.v2 codec failure: {type(exc).__name__}: {exc}',
                ) from exc
            finally:
                if broken and session is not None and self._health_session is session:
                    self._health_session = None
                    _SessionPool._close_quietly(session)
        finally:
            self._health_lock.release()

    def metrics(self, deadline: float | None = 2.0) -> Any:
        """Daemon-reported engine metrics plus the readiness budgets.

        Prefers the authenticated ``system.metrics`` control operation
        (listener/maintenance/packer counters, commit-stage totals, launch
        budgets). Older pinned binaries reject it as ``unknown_operation``
        and receive the previous synthetic envelope. Metrics never raise: an
        unreachable daemon degrades to the synthetic envelope, matching the
        pre-control-operation behavior callers rely on.
        """
        with self._readiness_lock:
            budget = dict(self._readiness_budget)
        try:
            reported = self._call('metrics', 'system.metrics', {}, deadline=deadline)
        except StorageError:
            reported = None
        if isinstance(reported, Mapping):
            metrics: dict[str, Any] = dict(reported)
            metrics['backend'] = 'tofudb'
            metrics['protocol'] = 'storage.v2'
            if budget and 'resourceBudget' not in metrics:
                metrics['resourceBudget'] = budget
            return metrics
        metrics = {
            'backend': 'tofudb',
            'protocol': 'storage.v2',
        }
        if budget:
            metrics['resourceBudget'] = budget
        return metrics

    def maintenance(
        self,
        operation: str,
        payload: Mapping[str, Any] | None = None,
        deadline: float | None = 30.0,
    ) -> Any:
        return self._call(
            'maintenance', operation, payload, deadline=deadline)


__all__ = ['StorageV2Client']
