"""Ordered in-process optional projections, never journal admission or authority.

Register under session admission; mark ready only after releasing it. A lane
exists only while synchronous callers or their reentrant callbacks are active.
No worker, timer, persistent queue, filesystem I/O, or durable state lives here.
Different files drain independently. Reentrant same-file callbacks queue behind
all listeners of the current change rather than recursively overtaking them.
"""
from contextlib import contextmanager
from collections import deque
from dataclasses import dataclass, field
import os
import threading
from typing import Callable

from lib.log import get_logger

logger = get_logger(__name__)
_condition = threading.Condition()
_lanes = {}

_publishing = threading.local()
_deferring = threading.local()

@dataclass(eq=False)
class _Lane:
    key: tuple
    pending: deque = field(default_factory=deque)
    owner: int | None = None


@dataclass(eq=False)
class _Ticket:
    lane: _Lane
    callback: Callable | None
    ready: bool = False
    done: bool = False


def _after_fork_child():
    # Parent callbacks belong to the parent's synchronous callers; never replay
    # them or inherit a condition potentially owned by a vanished parent thread.
    global _condition, _lanes, _publishing, _deferring
    _condition = threading.Condition()
    _lanes = {}
    _publishing = threading.local()
    _deferring = threading.local()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork_child)


def register(session_dir, key, callback):
    """Assign order under journal admission without executing or waiting."""
    lane_key = (session_dir, key)
    with _condition:
        lane = _lanes.setdefault(lane_key, _Lane(lane_key))
        ticket = _Ticket(lane, callback)
        lane.pending.append(ticket)
        return ticket


def _release_owner(lane):
    lane.owner = None
    if not lane.pending and _lanes.get(lane.key) is lane:
        del _lanes[lane.key]
    _condition.notify_all()


def _drain_until_done(requested):
    lane = requested.lane
    owner = threading.get_ident()
    while True:
        with _condition:
            if requested.done or lane.owner == owner:
                return
            if lane.owner is not None or not lane.pending[0].ready:
                _condition.wait()
                continue
            lane.owner = owner
        try:
            while True:
                with _condition:
                    if not lane.pending or not lane.pending[0].ready:
                        break
                    ticket = lane.pending.popleft()
                try:
                    ticket.callback()
                except Exception:
                    logger.warning('Optional project post-commit effect failed', exc_info=True)
                finally:
                    with _condition:
                        ticket.callback = None
                        ticket.done = True
                        _condition.notify_all()
        finally:
            # Includes cancellation/BaseException: another waiting caller can
            # take over, and no completed lane/closure stays in the registry.
            with _condition:
                _release_owner(lane)


@contextmanager
def defer_publication():
    """Keep tickets unready until a caller releases its filesystem leases.

    This postpones presentation only, never journal commit or history staging.
    Nested file operations join the outer release boundary. At fork, inherited
    callbacks belong to the parent and are neither replayed nor drained.
    """
    if getattr(_deferring, 'tickets', None) is not None:
        yield
        return
    owner_pid = os.getpid()
    tickets = _deferring.tickets = []
    try:
        yield
    finally:
        if os.getpid() == owner_pid:
            del _deferring.tickets
            publish(tickets)


def publish(tickets):
    """Complete synchronously; all reentrant work joins the outer caller's drain.

    Never wait on a second lane from inside a callback: another lane's callback
    may itself edit this path. Deferring nested work prevents that wait cycle.
    """
    deferred = getattr(_deferring, 'tickets', None)
    if deferred is not None:
        deferred.extend(tickets)
        return
    with _condition:
        for ticket in tickets:
            ticket.ready = True
        _condition.notify_all()
    pending = getattr(_publishing, 'pending', None)
    if pending is not None:
        pending.extend(tickets)
        return
    pending = _publishing.pending = deque(tickets)
    try:
        while pending:
            _drain_until_done(pending.popleft())
    finally:
        del _publishing.pending
