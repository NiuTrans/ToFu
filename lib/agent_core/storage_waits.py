"""Thread-local owner port for long in-call storage waits.

A supervised storage client waits out classified transient failures inside
the call while its authority is alive (docs/STORAGE.md, "Client retry
contract") and reports each long wait through ``observe_storage_wait``; the
caller that owns the wait enters ``storage_wait_owner`` to present it or end
it. Standard library only, so storage-free runs (the agent wheel, which
excludes ``lib.storage``) can enter an owner; ``lib.storage.retry`` re-exports
these objects for the storage client.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

# Owners stack per thread, not per ContextVar: event delivery runs each
# persist inside its producer's captured Context on a pump thread.
_wait_owners = threading.local()


@contextmanager
def storage_wait_owner(observe: Callable[[BaseException], None]) -> Iterator[None]:
    """Show this thread's long in-call storage waits to one owner.

    Once a call has waited as long as a slow storage call, the client reports
    every further wait to each owner entered on this thread, innermost first,
    before it pauses. ``observe(error)`` presents the wait; raising ends it
    (Stop, shutdown, or a caller that keeps its own bounded retry loop) and
    the exception propagates from the storage call.
    """
    owners = getattr(_wait_owners, 'stack', None)
    if owners is None:
        owners = _wait_owners.stack = []
    owners.append(observe)
    try:
        yield
    finally:
        owners.pop()


def observe_storage_wait(error: BaseException) -> None:
    """Report one long in-call wait to this thread's owners (client side)."""
    for observe in reversed(tuple(getattr(_wait_owners, 'stack', ()))):
        observe(error)


def decline_storage_wait(error: BaseException) -> None:
    """Owner for a caller whose own loop retries: end the in-call wait."""
    raise error


__all__ = ['decline_storage_wait', 'observe_storage_wait', 'storage_wait_owner']
