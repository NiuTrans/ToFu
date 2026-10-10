"""Revocable in-process publication authority; never serializes observer I/O.

Projection owners acquire their own mutation lock before publication_commit.
Captured child-owned contexts preserve originating authority across deferral.
Root FIFO acceptance may explicitly transfer it with accepted_publication_context.
Storage transactions still require their own attempt/CAS fence.
"""
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import threading

_publication_scope = ContextVar('child_publication_scope', default=None)


class ChildPublicationRevoked(RuntimeError):
    """A disposed child no longer owns downstream mutation authority."""


class ChildPublicationFence:
    def __init__(self):
        self._lock = threading.RLock()
        self._closed = False

    def close(self):
        with self._lock:
            self._closed = True

    def check(self):
        with self._lock:
            if self._closed:
                raise ChildPublicationRevoked('Child invocation publication revoked')

    def commit(self, mutation):
        with self._lock:
            self.check()
            return mutation()

    def deliver(self, sink, event):
        self.check()
        token = _publication_scope.set(self)
        try:
            return sink(event)
        finally:
            _publication_scope.reset(token)


@contextmanager
def publication_commit():
    """Only bounded local mutations belong here, after owner-lock acquisition."""
    fence = _publication_scope.get()
    if fence is None:
        yield
    else:
        with fence._lock:
            fence.check()
            yield


@contextmanager
def accepted_publication_context():
    """Fence local acceptance, then capture the receiving owner's context.

    The caller holds its queue/mutation lock and accepts inside this scope.
    Only the producer's child fence is removed from the detached context;
    principal and receiver attempt/CAS authority remain unchanged. Never use
    this for child-owned deferred work or arbitrary observer callbacks.
    """
    with publication_commit():
        context = copy_context()
        context.run(_publication_scope.set, None)
        yield context


def check_publication():
    fence = _publication_scope.get()
    if fence is not None:
        fence.check()


def commit_child_publication(mutation):
    with publication_commit():
        return mutation()
