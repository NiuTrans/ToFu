"""One resource-owning lifecycle for server-side execution.

``TaskRuntime.status`` remains the durable/user-visible task state.  This
module owns the orthogonal operational facts that used to be open-coded by
HTTP routes: route handles, billing reservations, admission leases, upstream
dispatch, monotonic execution phases, and exactly-once cleanup.

Entry points create or obtain one :class:`ExecutionSession`, register every
acquired resource immediately, and call :meth:`settle` before publishing a
terminal success.  Recoverable resources (for example a durable billing hold
or TTL-backed admission lease) may settle as ``deferred``; all other cleanup
failures violate the terminal invariant and turn a requested success into an
execution failure.
"""

from __future__ import annotations

import threading
import time
import weakref
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from lib.log import get_logger


logger = get_logger(__name__)


class ExecutionPhase(str, Enum):
    CREATED = "created"
    ROUTED = "routed"
    RESERVED = "reserved"
    ADMITTED = "admitted"
    DISPATCHING = "dispatching"
    SETTLING = "settling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


TERMINAL_EXECUTION_PHASES = frozenset({
    ExecutionPhase.COMPLETED,
    ExecutionPhase.FAILED,
    ExecutionPhase.CANCELLED,
    ExecutionPhase.TIMED_OUT,
})

_PRE_TERMINAL_ORDER = {
    ExecutionPhase.CREATED: 0,
    ExecutionPhase.ROUTED: 1,
    ExecutionPhase.RESERVED: 2,
    ExecutionPhase.ADMITTED: 3,
    ExecutionPhase.DISPATCHING: 4,
    ExecutionPhase.SETTLING: 5,
}
_MAX_RESOURCES_PER_EXECUTION = 16


class ResourceDisposition(str, Enum):
    RELEASED = "released"
    DEFERRED = "deferred"
    FAILED = "failed"


@dataclass(frozen=True)
class ExecutionSettlementContext:
    execution_id: str
    kind: str
    owner_user_id: int
    outcome: ExecutionPhase
    cause: str
    dispatch_started: bool


@dataclass(frozen=True)
class ExecutionTerminalReceipt:
    execution_id: str
    kind: str
    requested_outcome: ExecutionPhase
    outcome: ExecutionPhase
    cause: str
    invariants_satisfied: bool
    resource_dispositions: tuple[tuple[str, ResourceDisposition], ...]
    duration_seconds: float


@dataclass
class _OwnedResource:
    name: str
    release: Callable[[ExecutionSettlementContext], Any]
    release_order: int
    recoverable: bool
    disposition: ResourceDisposition | None = None


_active_sessions: weakref.WeakValueDictionary[str, "ExecutionSession"] = (
    weakref.WeakValueDictionary()
)
_active_sessions_lock = threading.Lock()


def _record_metric(name: str, *args, **kwargs) -> None:
    """Keep observability best-effort and acyclic for the execution hot path."""
    try:
        from lib import observability
        callback = getattr(observability, name)
        callback(*args, **kwargs)
    except Exception as exc:  # pragma: no cover - metrics never own execution
        logger.debug("execution metric %s skipped: %s", name, exc)


class ExecutionSession:
    """Thread-safe, monotonic owner of one execution's live resources."""

    def __init__(
        self,
        *,
        execution_id: str,
        kind: str,
        owner_user_id: int,
        request_id: str = "",
        deadline_seconds: float | None = None,
    ) -> None:
        if not str(execution_id or "").strip():
            raise ValueError("ExecutionSession requires execution_id")
        if isinstance(owner_user_id, bool) or int(owner_user_id) < 1:
            raise ValueError("ExecutionSession requires a positive owner_user_id")
        if deadline_seconds is not None and float(deadline_seconds) <= 0:
            raise ValueError("deadline_seconds must be positive")

        self.execution_id = str(execution_id)
        self.kind = str(kind or "execution")[:64]
        self.owner_user_id = int(owner_user_id)
        self.request_id = str(request_id or "")[:128]
        self.created_monotonic = time.monotonic()
        self.deadline_monotonic = (
            self.created_monotonic + float(deadline_seconds)
            if deadline_seconds is not None else None
        )
        self._phase = ExecutionPhase.CREATED
        self._phase_changed_monotonic = self.created_monotonic
        self._heartbeat_monotonic = self.created_monotonic
        self._dispatch_started = False
        self._cancel_requested = False
        self._cancel_reason = ""
        self._resources: dict[str, _OwnedResource] = {}
        self._terminal_receipt: ExecutionTerminalReceipt | None = None
        self._lock = threading.RLock()

        with _active_sessions_lock:
            _active_sessions[self.execution_id] = self
        _record_metric("record_execution_started", self.kind, self._phase.value)

    @property
    def phase(self) -> ExecutionPhase:
        with self._lock:
            return self._phase

    @property
    def dispatch_started(self) -> bool:
        with self._lock:
            return self._dispatch_started

    @property
    def cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel_requested

    @property
    def is_terminal(self) -> bool:
        with self._lock:
            return self._terminal_receipt is not None

    def _advance(self, target: ExecutionPhase) -> bool:
        with self._lock:
            current = self._phase
            if current in TERMINAL_EXECUTION_PHASES:
                return False
            if target in TERMINAL_EXECUTION_PHASES:
                raise ValueError("terminal transitions must use settle()")
            if _PRE_TERMINAL_ORDER[target] < _PRE_TERMINAL_ORDER[current]:
                raise ValueError(
                    f"execution phase cannot regress {current.value} -> {target.value}"
                )
            if target == current:
                self._heartbeat_monotonic = time.monotonic()
                return False
            self._phase = target
            self._phase_changed_monotonic = time.monotonic()
            self._heartbeat_monotonic = self._phase_changed_monotonic
        _record_metric(
            "record_execution_phase_transition",
            self.kind,
            current.value,
            target.value,
        )
        return True

    def mark_routed(self) -> bool:
        return self._advance(ExecutionPhase.ROUTED)

    def mark_reserved(self) -> bool:
        return self._advance(ExecutionPhase.RESERVED)

    def mark_admitted(self) -> bool:
        return self._advance(ExecutionPhase.ADMITTED)

    def mark_dispatch_started(self) -> bool:
        changed = self._advance(ExecutionPhase.DISPATCHING)
        with self._lock:
            self._dispatch_started = True
        return changed

    def heartbeat(self) -> None:
        with self._lock:
            if self._terminal_receipt is None:
                self._heartbeat_monotonic = time.monotonic()

    def request_cancel(self, reason: str) -> bool:
        with self._lock:
            if self._terminal_receipt is not None:
                return False
            first = not self._cancel_requested
            self._cancel_requested = True
            if first:
                self._cancel_reason = str(reason or "cancelled")[:96]
            return first

    def hold_resource(
        self,
        name: str,
        release: Callable[[ExecutionSettlementContext], Any],
        *,
        release_order: int = 0,
        recoverable: bool = False,
    ) -> None:
        resource_name = str(name or "").strip()
        if not resource_name or len(resource_name) > 64:
            raise ValueError("execution resource name must be 1..64 characters")
        if not callable(release):
            raise TypeError("execution resource release must be callable")
        with self._lock:
            if self._terminal_receipt is not None or self._phase == ExecutionPhase.SETTLING:
                raise RuntimeError("cannot acquire a resource while execution is settling")
            if resource_name in self._resources:
                raise ValueError(f"execution resource already held: {resource_name}")
            if len(self._resources) >= _MAX_RESOURCES_PER_EXECUTION:
                raise RuntimeError("execution resource budget exceeded")
            self._resources[resource_name] = _OwnedResource(
                name=resource_name,
                release=release,
                release_order=int(release_order),
                recoverable=bool(recoverable),
            )

    def settle(
        self,
        outcome: ExecutionPhase,
        *,
        cause: str = "",
    ) -> ExecutionTerminalReceipt:
        """Release every resource once and freeze a terminal invariant receipt."""
        if outcome not in TERMINAL_EXECUTION_PHASES:
            raise ValueError("execution outcome must be terminal")
        with self._lock:
            if self._terminal_receipt is not None:
                return self._terminal_receipt
            previous_phase = self._phase
            self._phase = ExecutionPhase.SETTLING
            self._phase_changed_monotonic = time.monotonic()
            context = ExecutionSettlementContext(
                execution_id=self.execution_id,
                kind=self.kind,
                owner_user_id=self.owner_user_id,
                outcome=outcome,
                cause=str(cause or "")[:128],
                dispatch_started=self._dispatch_started,
            )
            resources = sorted(
                self._resources.values(),
                key=lambda item: (-item.release_order, item.name),
            )

            if previous_phase != ExecutionPhase.SETTLING:
                _record_metric(
                    "record_execution_phase_transition",
                    self.kind,
                    previous_phase.value,
                    ExecutionPhase.SETTLING.value,
                )

            for resource in resources:
                if resource.disposition is not None:
                    continue
                try:
                    raw_disposition = resource.release(context)
                    if raw_disposition in (ResourceDisposition.DEFERRED, "deferred"):
                        resource.disposition = ResourceDisposition.DEFERRED
                    else:
                        resource.disposition = ResourceDisposition.RELEASED
                except Exception as exc:
                    resource.disposition = (
                        ResourceDisposition.DEFERRED
                        if resource.recoverable else ResourceDisposition.FAILED
                    )
                    logger.error(
                        "[ExecutionSession] resource cleanup failed "
                        "execution=%s kind=%s resource=%s recoverable=%s type=%s",
                        self.execution_id[:20], self.kind, resource.name,
                        resource.recoverable, type(exc).__name__,
                    )
                _record_metric(
                    "record_execution_resource_release",
                    self.kind,
                    resource.name,
                    resource.disposition.value,
                )

            dispositions = tuple(
                (resource.name, resource.disposition or ResourceDisposition.FAILED)
                for resource in resources
            )
            invariants_satisfied = all(
                disposition != ResourceDisposition.FAILED
                for _, disposition in dispositions
            )
            final_outcome = outcome
            final_cause = context.cause
            if not invariants_satisfied:
                final_outcome = ExecutionPhase.FAILED
                final_cause = final_cause or "terminal_resource_invariant_failed"
            now = time.monotonic()
            self._phase = final_outcome
            self._phase_changed_monotonic = now
            self._heartbeat_monotonic = now
            self._terminal_receipt = ExecutionTerminalReceipt(
                execution_id=self.execution_id,
                kind=self.kind,
                requested_outcome=outcome,
                outcome=final_outcome,
                cause=final_cause,
                invariants_satisfied=invariants_satisfied,
                resource_dispositions=dispositions,
                duration_seconds=max(0.0, now - self.created_monotonic),
            )

        with _active_sessions_lock:
            _active_sessions.pop(self.execution_id, None)
        _record_metric(
            "record_execution_terminal",
            self.kind,
            final_outcome.value,
            invariants_satisfied,
            self._terminal_receipt.duration_seconds,
        )
        return self._terminal_receipt

    def snapshot(self) -> dict[str, Any]:
        """Return content-free reconciliation state; never expose owner/request ids."""
        with self._lock:
            now = time.monotonic()
            return {
                "kind": self.kind,
                "phase": self._phase.value,
                "age_seconds": max(0.0, now - self.created_monotonic),
                "phase_age_seconds": max(0.0, now - self._phase_changed_monotonic),
                "heartbeat_age_seconds": max(0.0, now - self._heartbeat_monotonic),
                "deadline_remaining_seconds": (
                    None if self.deadline_monotonic is None
                    else self.deadline_monotonic - now
                ),
                "dispatch_started": self._dispatch_started,
                "cancel_requested": self._cancel_requested,
                "held_resources": tuple(sorted(
                    name for name, resource in self._resources.items()
                    if resource.disposition is None
                )),
            }


def execution_session_for_task(task: dict) -> ExecutionSession:
    session = task.get("_executionSession")
    if not isinstance(session, ExecutionSession):
        raise ValueError("task is missing its ExecutionSession")
    return session


def bind_model_route(
    session: ExecutionSession,
    release: Callable[[], Any],
) -> None:
    """Bind an already-minted request route to canonical terminal cleanup."""
    session.mark_routed()

    def _release(_context: ExecutionSettlementContext) -> None:
        release()

    session.hold_resource("model_route", _release, release_order=200)


def bind_billing_reservation(
    session: ExecutionSession,
    *,
    reservation_micro: int,
    settle: Callable[[], Any],
    release: Callable[[], Any],
) -> None:
    """Bind billing whether it settles actual usage or releases pre-dispatch."""
    if int(reservation_micro or 0) > 0:
        session.mark_reserved()

    def _release(context: ExecutionSettlementContext) -> None:
        result = settle() if context.dispatch_started else release()
        if result is False:
            raise RuntimeError("billing operation did not acknowledge settlement")

    session.hold_resource(
        "billing", _release, release_order=300, recoverable=True,
    )


def bind_admission_lease(
    session: ExecutionSession,
    release: Callable[[], Any],
) -> None:
    """Bind an explicit TTL-backed admission lease to one execution."""
    session.mark_admitted()

    def _release(_context: ExecutionSettlementContext) -> None:
        result = release()
        if result is False:
            raise RuntimeError("admission lease release was not acknowledged")

    session.hold_resource(
        "admission", _release, release_order=100, recoverable=True,
    )


def execution_outcome_for_task(task: dict, event: dict | None = None) -> ExecutionPhase:
    status = str(task.get("status") or "")
    finish_reason = str(task.get("finishReason") or "")
    if status == "error" or (event or {}).get("error"):
        return ExecutionPhase.FAILED
    if status in {"aborted", "interrupted"} or finish_reason in {
        "aborted", "cancelled", "interrupted",
    }:
        return ExecutionPhase.CANCELLED
    if finish_reason in {"timeout", "timed_out"}:
        return ExecutionPhase.TIMED_OUT
    return ExecutionPhase.COMPLETED


def settle_task_execution(
    task: dict,
    *,
    event: dict | None = None,
    cause: str = "",
) -> ExecutionTerminalReceipt:
    session = execution_session_for_task(task)
    return session.settle(
        execution_outcome_for_task(task, event),
        cause=cause or str(task.get("_abort_reason") or ""),
    )


def active_execution_snapshots(*, limit: int = 256) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(1024, int(limit)))
    with _active_sessions_lock:
        sessions = list(_active_sessions.values())[:bounded_limit]
    return [session.snapshot() for session in sessions]


def reconcile_overdue_execution_sessions() -> int:
    """Request cancellation for deadline-expired work; owners still unwind it.

    The reconciler never releases resources underneath a live provider/tool
    call.  It raises the shared cancellation flag; the bounded dispatch owner
    observes that flag and performs the ordinary exactly-once settlement path.
    """
    now = time.monotonic()
    with _active_sessions_lock:
        sessions = list(_active_sessions.values())
    overdue = 0
    for session in sessions:
        deadline = session.deadline_monotonic
        if deadline is None or deadline > now or session.is_terminal:
            continue
        if session.request_cancel("execution_deadline_exceeded"):
            overdue += 1
            _record_metric("record_execution_deadline", session.kind)
    return overdue


__all__ = [
    "ExecutionPhase",
    "ExecutionSession",
    "ExecutionSettlementContext",
    "ExecutionTerminalReceipt",
    "ResourceDisposition",
    "TERMINAL_EXECUTION_PHASES",
    "active_execution_snapshots",
    "bind_admission_lease",
    "bind_billing_reservation",
    "bind_model_route",
    "execution_outcome_for_task",
    "execution_session_for_task",
    "reconcile_overdue_execution_sessions",
    "settle_task_execution",
]
