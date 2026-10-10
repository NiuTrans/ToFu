"""lib/llm_dispatch/factory.py — Factory pattern for LLM dispatcher creation.

Provides a ``DispatcherFactory`` that encapsulates singleton lifecycle
management for ``LLMDispatcher`` instances, and module-level convenience
functions ``get_dispatcher()`` / ``reset_dispatcher()`` that delegate to
the default factory.

Usage:
    from lib.llm_dispatch import get_dispatcher, reset_dispatcher

    dispatcher = get_dispatcher()          # lazy-create singleton
    reset_dispatcher()                     # force re-init (e.g. after benchmark refresh)
"""

import threading

from lib.log import get_logger

from .dispatcher import LLMDispatcher

logger = get_logger(__name__)

__all__ = [
    'DispatcherFactory',
    'get_dispatcher',
    'reset_dispatcher',
]


class DispatcherFactory:
    """Thread-safe factory that manages LLMDispatcher singleton lifecycle.

    Implements the Factory pattern: callers request a dispatcher instance
    via ``create()`` and the factory decides whether to return the existing
    singleton or build a new one.

    This is useful for:
      - Lazy initialization (dispatcher is only built on first use)
      - Controlled reset (e.g. after benchmark data refresh)
      - Testing (swap in a mock dispatcher)
    """

    def __init__(self):
        self._instance: LLMDispatcher | None = None
        self._lock = threading.Lock()

    def create(self) -> LLMDispatcher:
        """Get or create the LLMDispatcher singleton.

        Thread-safe with double-checked locking.
        """
        if self._instance is None:
            with self._lock:
                if self._instance is None:
                    self._instance = LLMDispatcher()
        return self._instance

    def reset(self):
        """Invalidate configured routes without orphaning active requests.

        Keep the singleton: in-flight dispatches, ephemeral handles and later
        rounds must all refer to the same pool through a configuration reload.
        An unused factory remains lazy.
        """
        with self._lock:
            if self._instance is not None:
                self._instance.invalidate_configuration()
        logger.info('Dispatcher configuration reset — live request routes '
                    'retained; will re-initialize on next call')

# ── Default global factory ──
_default_factory = DispatcherFactory()


def get_dispatcher() -> LLMDispatcher:
    """Get or create the global dispatcher singleton."""
    dispatcher = _default_factory.create()
    from lib.resource_control.adapters import bind_dispatcher
    bind_dispatcher(dispatcher)
    return dispatcher


def reset_dispatcher():
    """Invalidate configured slots so the next pick rebuilds them from v2.

    Public lifecycle port for committed model-routing mutations. In-package
    health/autodiscover uses the same invalidation before immediate rebuild.
    Active request routes retain their identity and teardown ownership.
    """
    _default_factory.reset()
