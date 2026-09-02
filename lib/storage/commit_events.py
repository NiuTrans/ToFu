"""Process-local delivery of storage events after their transaction commits.

Responsibility: bridge the private sidecar command envelope to application
observers without coupling the storage client to conversation or HTTP code.
Durability and replay remain in the database; subscribers are wakeup hints.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import threading
from typing import Any

from lib.log import get_logger


logger = get_logger(__name__)

CommittedEventHandler = Callable[[tuple[Mapping[str, Any], ...]], None]

_handlers: set[CommittedEventHandler] = set()
_handlers_lock = threading.RLock()


def subscribe_committed_events(handler: CommittedEventHandler) -> Callable[[], None]:
    """Subscribe to post-commit wakeups and return an idempotent unsubscribe."""
    with _handlers_lock:
        _handlers.add(handler)

    def unsubscribe() -> None:
        with _handlers_lock:
            _handlers.discard(handler)

    return unsubscribe


def publish_committed_events(events: Sequence[Mapping[str, Any]]) -> None:
    """Notify current-process observers; never invalidate a committed command."""
    immutable_events = tuple(dict(event) for event in events)
    if not immutable_events:
        return
    with _handlers_lock:
        handlers = tuple(_handlers)
    for handler in handlers:
        try:
            handler(immutable_events)
        except Exception:
            # The durable log is the source of truth.  A failed hint can delay
            # a reader until its heartbeat probe, but must not turn a committed
            # mutation into an apparent command failure and invite a replay.
            logger.exception("Committed-event subscriber failed")


__all__ = ["publish_committed_events", "subscribe_committed_events"]
