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
from typing import Optional

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
        """Discard the current singleton so the next ``create()`` builds fresh.

        Useful after benchmark data is refreshed or configuration changes.
        """
        with self._lock:
            self._instance = None
        logger.info('Dispatcher reset — will re-initialize on next call')

    def set_instance(self, dispatcher: LLMDispatcher):
        """Replace the singleton with a custom instance (e.g. for testing).

        Args:
            dispatcher: A pre-configured LLMDispatcher (or mock).
        """
        with self._lock:
            self._instance = dispatcher


# ── Default global factory ──
_default_factory = DispatcherFactory()


def get_dispatcher() -> LLMDispatcher:
    """Get or create the global dispatcher singleton."""
    return _default_factory.create()


def reset_dispatcher():
    """Reset the dispatcher (e.g. after benchmark refresh)."""
    _default_factory.reset()
