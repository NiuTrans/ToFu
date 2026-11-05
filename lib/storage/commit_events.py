"""Process-local delivery of storage events after their transaction commits.

Responsibility: bridge the private sidecar command envelope to application
observers without coupling the storage client to conversation or HTTP code,
and name each acknowledged command operation to the observers that asked for
it. Durability and replay remain in the database; subscribers are wakeup hints.
A writer that commits inside its own critical section enters
``defer_committed_events`` outside that section: its notices then publish
after the section ends, in the order the writer committed them.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
import threading
from typing import Any

from lib.log import get_logger


logger = get_logger(__name__)

CommittedEventHandler = Callable[[tuple[Mapping[str, Any], ...]], None]
CommittedOperationHandler = Callable[[str], None]

_handlers: set[CommittedEventHandler] = set()
_handlers_lock = threading.RLock()
_deferral = threading.local()


class CommittedEventOrder:
    """FIFO of one writer's commit notices, published by one thread at a time.

    Notices join the FIFO inside the writer's critical section, so FIFO order
    is commit order. A drain that finds another thread publishing leaves the
    FIFO to it; that publisher re-checks the FIFO before giving up the role,
    so no deferred notice is stranded and no two notices publish out of order.
    """

    __slots__ = ('_lock', '_pending', '_publishing')

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: deque[tuple[Mapping[str, Any], ...]] = deque()
        self._publishing = False

    def _defer(self, events: tuple[Mapping[str, Any], ...]) -> None:
        with self._lock:
            self._pending.append(events)

    def _publish_pending(self) -> None:
        while True:
            with self._lock:
                if self._publishing or not self._pending:
                    return
                self._publishing = True
                events = self._pending.popleft()
            try:
                _dispatch(events)
            finally:
                with self._lock:
                    self._publishing = False


# Replaced copy-on-write under the lock; publication reads one immutable tuple.
_operation_handlers: dict[str, tuple[CommittedOperationHandler, ...]] = {}


def subscribe_committed_events(handler: CommittedEventHandler) -> Callable[[], None]:
    """Subscribe to post-commit wakeups and return an idempotent unsubscribe."""
    with _handlers_lock:
        _handlers.add(handler)

    def unsubscribe() -> None:
        with _handlers_lock:
            _handlers.discard(handler)

    return unsubscribe


@contextmanager
def defer_committed_events(order: CommittedEventOrder) -> Iterator[None]:
    """Hold this thread's commit notices in ``order`` until the outermost scope exits.

    Enter the scope before the writer's lock so that its exit, which
    publishes, runs after the lock is released. Nested scopes publish nothing
    until the outermost one exits; failures inside the scope still publish
    notices of commands that committed.
    """
    scopes = getattr(_deferral, 'scopes', None)
    if scopes is None:
        scopes = _deferral.scopes = []
    outermost = not scopes
    if outermost:
        _deferral.orders = []
    if order not in _deferral.orders:
        _deferral.orders.append(order)
    scopes.append(order)
    try:
        yield
    finally:
        scopes.pop()
        if outermost:
            orders, _deferral.orders = _deferral.orders, []
            for deferred in orders:
                deferred._publish_pending()


def publish_committed_events(events: Sequence[Mapping[str, Any]]) -> None:
    """Notify current-process observers; never invalidate a committed command.

    Inside ``defer_committed_events`` the notices join the innermost scope's
    order and publish when the outermost scope exits.
    """
    immutable_events = tuple(dict(event) for event in events)
    if not immutable_events:
        return
    scopes = getattr(_deferral, 'scopes', None)
    if scopes:
        scopes[-1]._defer(immutable_events)
        return
    _dispatch(immutable_events)


def _dispatch(events: tuple[Mapping[str, Any], ...]) -> None:
    with _handlers_lock:
        handlers = tuple(_handlers)
    for handler in handlers:
        try:
            handler(events)
        except Exception:
            # The durable log is the source of truth.  A failed hint can delay
            # a reader until its heartbeat probe, but must not turn a committed
            # mutation into an apparent command failure and invite a replay.
            logger.exception("Committed-event subscriber failed")


def subscribe_committed_operations(
    operations: Iterable[str], handler: CommittedOperationHandler,
) -> Callable[[], None]:
    """Call ``handler(operation)`` after a listed command is acknowledged here.

    Returns an idempotent unsubscribe. Other processes' commits are invisible.
    """
    names = frozenset(operations)
    with _handlers_lock:
        for name in names:
            _operation_handlers[name] = (*_operation_handlers.get(name, ()), handler)

    def unsubscribe() -> None:
        with _handlers_lock:
            for name in names:
                remaining = tuple(
                    current for current in _operation_handlers.get(name, ())
                    if current is not handler)
                if remaining:
                    _operation_handlers[name] = remaining
                else:
                    _operation_handlers.pop(name, None)

    return unsubscribe


def publish_committed_operation(operation: str) -> None:
    """Name one acknowledged command to its subscribers; never fail the command."""
    for handler in _operation_handlers.get(operation, ()):
        try:
            handler(operation)
        except Exception:
            logger.exception("Committed-operation subscriber failed")


__all__ = [
    "CommittedEventOrder",
    "defer_committed_events",
    "publish_committed_events",
    "publish_committed_operation",
    "subscribe_committed_events",
    "subscribe_committed_operations",
]
