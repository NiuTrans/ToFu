"""Caller-owned lazy HTML capability; no import-time native load or selection.

LazyHtmlProjector accepts an explicit pinned binding factory. Calls join one
initialization, lease it through projection, and close waits for every accepted
call before releasing the binding. Initialization failure stays failed, without
retaining input-bearing tracebacks or silently retrying/falling back.
"""
import threading


class HtmlProjectionClosed(RuntimeError):
    """The owned projection service cannot accept further work."""


class LazyHtmlProjector:
    def __init__(self, factory):
        if not callable(factory):
            raise TypeError('HTML binding factory must be callable')
        self._factory = factory
        self._condition = threading.Condition()
        self._state = 'open'
        self._active = 0
        self._active_threads = {}
        self._loading = False
        self._loading_thread = None
        self._initialization_failed = False
        self._binding = None
        self._profile = None

    @property
    def profile(self):
        """Observed immutable build facts, or None before any native use."""
        with self._condition:
            return self._profile

    def project(self, value):
        with self._condition:
            if self._state != 'open':
                raise HtmlProjectionClosed('HTML projection service closed')
            self._active += 1
            thread = threading.get_ident()
            self._active_threads[thread] = self._active_threads.get(thread, 0) + 1
            self._condition.notify_all()
        try:
            with self._condition:
                while self._loading:
                    if self._loading_thread == threading.get_ident():
                        raise HtmlProjectionClosed('HTML initialization cannot reenter projection')
                    self._condition.wait()
                if self._initialization_failed:
                    raise HtmlProjectionClosed('HTML projection initialization previously failed')
                initialize = self._binding is None
                if initialize:
                    self._loading = True
                    self._loading_thread = threading.get_ident()
            if initialize:
                binding = None
                published = False
                try:
                    binding = self._factory()
                    profile = binding.profile
                    with self._condition:
                        self._binding = binding
                        self._profile = profile
                        published = True
                except BaseException as primary:
                    if published:
                        # The service owns the complete binding even if this
                        # caller is interrupted while leaving publication.
                        raise
                    try:
                        if binding is not None:
                            binding.close()
                        with self._condition:
                            self._binding = None
                            self._profile = None
                    except BaseException as cleanup:
                        # Retain ownership when initialization rollback fails.
                        with self._condition:
                            self._binding = binding
                        raise BaseExceptionGroup('HTML initialization and rollback failed',
                                                 [primary, cleanup]) from None
                    finally:
                        with self._condition:
                            self._initialization_failed = True
                    raise
                finally:
                    with self._condition:
                        self._factory = None
                        self._loading = False
                        self._loading_thread = None
                        self._condition.notify_all()
            return self._binding.project(value)
        finally:
            with self._condition:
                self._active -= 1
                self._active_threads[thread] -= 1
                if not self._active_threads[thread]:
                    del self._active_threads[thread]
                self._condition.notify_all()

    def validate_close_caller(self):
        """Refuse self-join before a caller hands close to another thread."""
        with self._condition:
            if threading.get_ident() in self._active_threads:
                raise HtmlProjectionClosed('HTML projection cannot close its own service')

    def close(self):
        self.validate_close_caller()
        with self._condition:
            while self._state == 'closing':
                self._condition.wait()
            if self._state == 'closed':
                return
            if self._state == 'failed':
                raise HtmlProjectionClosed('HTML projection close failed; binding retained')
            self._state = 'closing'
            self._condition.notify_all()
            try:
                while self._active:
                    self._condition.wait()
            except BaseException:
                self._state = 'open'
                self._condition.notify_all()
                raise
        try:
            if self._binding is not None:
                self._binding.close()
        except BaseException:
            with self._condition:
                self._state = 'failed'
                self._condition.notify_all()
            raise
        with self._condition:
            self._binding = self._factory = None
            self._state = 'closed'
            self._condition.notify_all()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
