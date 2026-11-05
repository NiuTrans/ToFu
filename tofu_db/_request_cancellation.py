"""Cancellation of one exclusively owned transport exchange.

Bindings detach before a socket can return to its pool. Cancellation holds the
same lock through the nonblocking interrupt, so a captured callback cannot fire
after ownership has moved to another request. No thread, timer or wire command.
"""
from contextlib import contextmanager
import threading


class RequestCancellation:
    def __init__(self):
        self._lock = threading.Lock()
        self._cancelled = False
        self._interrupt = None

    @property
    def cancelled(self):
        with self._lock:
            return self._cancelled

    def cancel(self):
        with self._lock:
            self._cancelled = True
            if self._interrupt is not None:
                try:
                    self._interrupt()
                except OSError:
                    pass  # The exclusively owned peer may already have closed.

    @contextmanager
    def bind(self, interrupt):
        with self._lock:
            if self._cancelled:
                raise OSError('Transport request was cancelled before dispatch')
            if self._interrupt is not None:
                raise RuntimeError('Cancellation already owns a transport exchange')
            self._interrupt = interrupt
        try:
            yield
        finally:
            with self._lock:
                self._interrupt = None
